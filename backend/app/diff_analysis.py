from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import re
import select
import shutil
import signal
import stat
import subprocess
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .graph import GraphStore
from .graph_tools import DeterministicGraphTools, GraphToolRequest, ImpactToolRequest
from .ids import stable_id
from .ingestion import (
    RepositoryError,
    identify_snapshot,
    inspect_repository,
    load_snapshot_analysis,
)
from .jobs import IngestionJobManager
from .lifecycle import register_active_snapshot, unregister_active_snapshot
from .models import (
    GitChangeStatus,
    IngestionJobStatus,
    RepositoryInfo,
    SnapshotAnalysis,
    SnapshotStatus,
    TestDefinition,
)

MAX_DIFF_PATHS = 2_000
MAX_DIFF_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_RECOMMENDATIONS = 500
MAX_DIFF_SYMBOLS = 10_000
MAX_IMPACT_ANCHORS = 100
MAX_TOTAL_IMPACT_PATHS = 2_000
RENAME_THRESHOLD = 50
DEFAULT_M21_MAX_BLOB_BYTES = 32 * 1024 * 1024
DEFAULT_M21_MAX_MATERIALIZED_BYTES = 256 * 1024 * 1024
DEFAULT_M21_MATERIALIZATION_TIMEOUT_SECONDS = 120
_SHA_PREFIX = re.compile(r"^[0-9a-fA-F]{7,40}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class M21DiffError(ValueError):
    """A commit diff cannot be safely or deterministically analyzed."""


class DiffRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_line: int = Field(ge=0)
    line_count: int = Field(ge=0)


class ChangedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    status: GitChangeStatus
    old_path: str | None = None
    new_path: str | None = None
    rename_similarity: int | None = Field(default=None, ge=0, le=100)
    base_ranges: tuple[DiffRange, ...] = ()
    target_ranges: tuple[DiffRange, ...] = ()


class DiffEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_type: Literal[
        "SOURCE_RANGE",
        "VERIFIED_GRAPH_PATH",
        "CONSERVATIVE_GRAPH_PATH",
        "RUNTIME_OBSERVATION",
        "TEST_COLLECTION",
    ]
    snapshot_id: str
    record_id: str
    evidence_id: str | None = None
    file_path: str | None = None
    content_hash: str | None = None
    runtime_materialization_id: str | None = None
    runtime_environment_id: str | None = None
    run_id: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    entity_id: str | None = None


class ChangedSymbol(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    classification: Literal["ADDED", "DELETED", "MODIFIED", "RENAMED_OR_MOVED", "UNMAPPED"]
    base_snapshot_id: str
    target_snapshot_id: str
    base_entity_id: str | None = None
    target_entity_id: str | None = None
    base_qualified_name: str | None = None
    target_qualified_name: str | None = None
    base_file_path: str | None = None
    target_file_path: str | None = None
    deterministic_reason: str
    evidence: tuple[DiffEvidence, ...] = ()


class ImpactPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    snapshot_id: str
    changed_entity_id: str
    dependent_entity_id: str
    distance: int = Field(ge=1, le=5)
    mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"]
    path_semantics: Literal["STRICT_EXACT", "CONSERVATIVE", "INCOMPLETE"]
    evidence_complete: bool
    uncertainty_count: int = Field(ge=0)
    path: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    evidence: tuple[DiffEvidence, ...]
    unresolved_barriers: tuple[dict[str, Any], ...] = ()


class TestRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    test_definition_id: str | None = None
    test_invocation_id: str | None = None
    pytest_node_id: str | None = None
    tier: Literal["A", "B", "C"]
    label: Literal["RUNTIME_DIRECT", "RUNTIME_DEPENDENT", "COARSE_HEURISTIC"]
    deterministic_reason: str
    evidence: tuple[DiffEvidence, ...]
    impact_path: ImpactPath | None = None


class TestPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    runtime_materialization_id: str | None = None
    runtime_environment_id: str | None = None
    runtime_baseline_available: bool
    collection_status: Literal["COMPLETED", "UNAVAILABLE", "FAILED"]
    tiers: dict[str, str]
    recommendations: tuple[TestRecommendation, ...] = ()
    diagnostics: tuple[str, ...] = ()


class DiffAnalysisResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    base_commit_sha: str
    target_commit_sha: str
    base_snapshot_id: str
    target_snapshot_id: str
    diff_mode: Literal["COMMIT_TO_COMMIT"] = "COMMIT_TO_COMMIT"
    rename_detection_threshold: int = RENAME_THRESHOLD
    changed_files: tuple[ChangedFile, ...]
    changed_symbols: tuple[ChangedSymbol, ...]
    impact_paths: tuple[ImpactPath, ...]
    test_plan: TestPlan
    candidate_count_before_limit: int = Field(ge=0)
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


class DiffAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_path: str = Field(min_length=1, max_length=4096)
    base_sha: str = Field(min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]+$")
    target_sha: str = Field(min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]+$")
    hop_limit: int = Field(default=3, ge=1, le=5)
    result_limit: int = Field(default=100, ge=1, le=500)


def _git(root: Path, *args: str, timeout: float = 120) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        if process.stdout is None:
            raise M21DiffError("Git diff output is unavailable")
        stdout_fd = process.stdout.fileno()
        deadline = time.monotonic() + timeout
        output = bytearray()
        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise M21DiffError("Git diff analysis exceeded its wall-clock deadline")
            readable, _, _ = select.select([stdout_fd], [], [], remaining_time)
            if not readable:
                raise M21DiffError("Git diff analysis exceeded its wall-clock deadline")
            chunk = os.read(stdout_fd, min(64 * 1024, MAX_DIFF_OUTPUT_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_DIFF_OUTPUT_BYTES:
                raise M21DiffError("Git diff output exceeds the M21 bounded analysis limit")
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise M21DiffError("Git diff analysis exceeded its wall-clock deadline")
        try:
            status = process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired as exc:
            raise M21DiffError("Git diff analysis exceeded its wall-clock deadline") from exc
        if status:
            raise M21DiffError("Git could not resolve or compare the requested commits")
        return bytes(output)
    except BaseException as exc:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        if isinstance(exc, (M21DiffError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, OSError):
            raise M21DiffError("Git diff analysis could not start or read output") from exc
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _resolve_commit(root: Path, value: str) -> str:
    if not _SHA_PREFIX.fullmatch(value):
        raise M21DiffError("Commit identifiers must be hexadecimal SHA prefixes")
    resolved = _git(root, "rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}")
    sha = resolved.decode("ascii", "strict").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise M21DiffError("Git returned an invalid commit identity")
    return sha


def _parse_changed_files(
    root: Path, repository_id: str, base_sha: str, target_sha: str
) -> list[ChangedFile]:
    output = _git(
        root,
        "diff",
        "--name-status",
        "-z",
        f"-M{RENAME_THRESHOLD}%",
        "--no-ext-diff",
        "--no-textconv",
        base_sha,
        target_sha,
    )
    try:
        fields = output.decode("utf-8", "strict").split("\0")
    except UnicodeDecodeError as exc:
        raise M21DiffError("Git paths must be valid UTF-8 for deterministic analysis") from exc
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[ChangedFile] = []
    cursor = 0
    while cursor < len(fields):
        status_value = fields[cursor]
        cursor += 1
        status = {
            "A": GitChangeStatus.ADDED,
            "M": GitChangeStatus.MODIFIED,
            "D": GitChangeStatus.DELETED,
            "R": GitChangeStatus.RENAMED,
            "C": GitChangeStatus.COPIED,
            "T": GitChangeStatus.TYPE_CHANGED,
        }.get(status_value[:1])
        if status is None:
            raise M21DiffError("Git returned an unsupported file change status")
        similarity = int(status_value[1:]) if status_value[1:].isdigit() else None
        old_path: str | None
        new_path: str | None
        if status in {GitChangeStatus.RENAMED, GitChangeStatus.COPIED}:
            if cursor + 1 >= len(fields):
                raise M21DiffError("Git returned a truncated rename record")
            old_path, new_path = fields[cursor : cursor + 2]
            cursor += 2
        else:
            if cursor >= len(fields):
                raise M21DiffError("Git returned a truncated file change record")
            path = fields[cursor]
            cursor += 1
            old_path = (
                path
                if status
                in {
                    GitChangeStatus.MODIFIED,
                    GitChangeStatus.DELETED,
                    GitChangeStatus.TYPE_CHANGED,
                }
                else None
            )
            new_path = (
                path
                if status
                in {
                    GitChangeStatus.ADDED,
                    GitChangeStatus.MODIFIED,
                    GitChangeStatus.TYPE_CHANGED,
                }
                else None
            )
        for value in (old_path, new_path):
            if value is not None:
                relative_path = PurePosixPath(value)
                if (
                    relative_path.is_absolute()
                    or ".." in relative_path.parts
                    or ".git" in relative_path.parts
                ):
                    raise M21DiffError("Git diff produced an unsafe repository path")
        change_id = stable_id(
            repository_id,
            base_sha,
            target_sha,
            status_value,
            old_path or "",
            new_path or "",
            prefix="diff_",
        )
        changes.append(
            ChangedFile(
                change_id=change_id,
                status=status,
                old_path=old_path,
                new_path=new_path,
                rename_similarity=similarity,
            )
        )
        if len(changes) > MAX_DIFF_PATHS:
            raise M21DiffError("Diff changes more than the M21 limit of 2,000 paths")
    return sorted(
        changes, key=lambda item: (item.old_path or "", item.new_path or "", item.change_id)
    )


def _ranges_for_path(
    root: Path, base_sha: str, target_sha: str, path: str | None
) -> tuple[list[DiffRange], list[DiffRange]]:
    if path is None:
        return [], []
    pathspec = f":(literal){path}"
    output = _git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--unified=0",
        base_sha,
        target_sha,
        "--",
        pathspec,
    )
    old_ranges: list[DiffRange] = []
    new_ranges: list[DiffRange] = []
    for line in output.decode("utf-8", "replace").splitlines():
        match = _HUNK.match(line)
        if not match:
            continue
        old_start, old_count, new_start, new_count = match.groups()
        old_ranges.append(DiffRange(start_line=int(old_start), line_count=int(old_count or 1)))
        new_ranges.append(DiffRange(start_line=int(new_start), line_count=int(new_count or 1)))
    return old_ranges, new_ranges


def _with_ranges(root: Path, base_sha: str, target_sha: str, change: ChangedFile) -> ChangedFile:
    old_ranges, _ = _ranges_for_path(root, base_sha, target_sha, change.old_path)
    _, new_ranges = _ranges_for_path(root, base_sha, target_sha, change.new_path)
    return change.model_copy(
        update={"base_ranges": tuple(old_ranges), "target_ranges": tuple(new_ranges)}
    )


def _intersects(entity: Any, ranges: Sequence[DiffRange]) -> bool:
    return any(
        item.line_count > 0
        and entity.span.start_line <= item.start_line + item.line_count - 1
        and entity.span.end_line >= item.start_line
        for item in ranges
    )


def _most_specific_changed_symbols(
    entities: Sequence[Any], ranges: Sequence[DiffRange]
) -> list[Any]:
    candidates = (
        [item for item in entities if _intersects(item, ranges)] if ranges else list(entities)
    )
    candidate_ids = {item.entity_id for item in candidates}
    nested_parents = {item.parent_id for item in candidates if item.parent_id in candidate_ids}
    return [item for item in candidates if item.entity_id not in nested_parents]


def _symbol_signature(entity: Any, analysis: SnapshotAnalysis) -> tuple[str, ...]:
    entities = {item.entity_id: item for item in analysis.entities}
    lineage: list[str] = [entity.kind, entity.name]
    parent = entities.get(entity.parent_id or "")
    while parent is not None and parent.kind != "Module":
        lineage.extend((parent.kind, parent.name))
        parent = entities.get(parent.parent_id or "")
    return tuple(lineage)


def _unchanged_symbol_ids(
    base: SnapshotAnalysis,
    target: SnapshotAnalysis,
    base_loader: Callable[[str], bytes | None] | None,
    target_loader: Callable[[str], bytes | None] | None,
) -> tuple[set[str], set[str]]:
    """Return uniquely matched same-path symbols with identical source bytes."""
    base_groups: dict[tuple[str, tuple[str, ...], str], list[Any]] = defaultdict(list)
    target_groups: dict[tuple[str, tuple[str, ...], str], list[Any]] = defaultdict(list)

    def fingerprint(entity: Any, analysis: SnapshotAnalysis, loader: Any) -> str | None:
        if loader is not None:
            content = loader(entity.file_path)
            if content is not None and 0 <= entity.span.start_byte <= entity.span.end_byte <= len(
                content
            ):
                return hashlib.sha256(
                    content[entity.span.start_byte : entity.span.end_byte]
                ).hexdigest()
        entry = next((item for item in analysis.manifest if item.path == entity.file_path), None)
        if entry is None or not entry.content_hash:
            return None
        return f"{entry.content_hash}:{entity.span.start_byte}:{entity.span.end_byte}"

    for analysis, loader, groups in (
        (base, base_loader, base_groups),
        (target, target_loader, target_groups),
    ):
        for entity in analysis.entities:
            if entity.kind not in {"Class", "Function", "Method"}:
                continue
            source_fingerprint = fingerprint(entity, analysis, loader)
            if source_fingerprint is None:
                continue
            fingerprint_key = (
                entity.file_path,
                _symbol_signature(entity, analysis),
                source_fingerprint,
            )
            groups[fingerprint_key].append(entity)
    unchanged_base: set[str] = set()
    unchanged_target: set[str] = set()
    for fingerprint_key in sorted(set(base_groups) & set(target_groups)):
        if len(base_groups[fingerprint_key]) == len(target_groups[fingerprint_key]) == 1:
            unchanged_base.add(base_groups[fingerprint_key][0].entity_id)
            unchanged_target.add(target_groups[fingerprint_key][0].entity_id)
    return unchanged_base, unchanged_target


def _source_evidence(snapshot: SnapshotAnalysis, entity: Any) -> DiffEvidence:
    entry = next((item for item in snapshot.manifest if item.path == entity.file_path), None)
    return DiffEvidence(
        evidence_type="SOURCE_RANGE",
        snapshot_id=snapshot.snapshot_id,
        record_id=stable_id(
            snapshot.snapshot_id,
            entity.entity_id,
            entity.span.start_byte,
            entity.span.end_byte,
            prefix="source_range_",
        ),
        file_path=entity.file_path,
        content_hash=entry.content_hash if entry else None,
        start_line=entity.span.start_line,
        end_line=entity.span.end_line,
        entity_id=entity.entity_id,
    )


def map_changed_symbols(
    repository_id: str,
    base_sha: str,
    target_sha: str,
    base: SnapshotAnalysis,
    target: SnapshotAnalysis,
    changed_files: Sequence[ChangedFile],
    *,
    base_source_loader: Callable[[str], bytes | None] | None = None,
    target_source_loader: Callable[[str], bytes | None] | None = None,
) -> list[ChangedSymbol]:
    """Map exact changed line intervals to symbols in each immutable snapshot."""
    if base.snapshot_id == target.snapshot_id and base_sha != target_sha:
        raise M21DiffError("Different commits unexpectedly resolved to the same snapshot identity")
    symbols: list[ChangedSymbol] = []
    # Pair logical symbols that remain at the same repository path before
    # looking for moves. Entity IDs include snapshot identity, so compare their
    # source text and structural lineage instead. Matching is only used to
    # exclude still-present symbols from cross-file move candidates.
    base_by_location: dict[tuple[str, tuple[str, ...]], list[Any]] = defaultdict(list)
    target_by_location: dict[tuple[str, tuple[str, ...]], list[Any]] = defaultdict(list)
    for entity in base.entities:
        if entity.kind in {"Class", "Function", "Method"}:
            base_by_location[(entity.file_path, _symbol_signature(entity, base))].append(entity)
    for entity in target.entities:
        if entity.kind in {"Class", "Function", "Method"}:
            target_by_location[(entity.file_path, _symbol_signature(entity, target))].append(entity)
    unchanged_base_ids, unchanged_target_ids = _unchanged_symbol_ids(
        base, target, base_source_loader, target_source_loader
    )
    still_present_base_ids: set[str] = set(unchanged_base_ids)
    still_present_target_ids: set[str] = set(unchanged_target_ids)
    for location_key in sorted(set(base_by_location) & set(target_by_location)):
        still_present_base_ids.update(item.entity_id for item in base_by_location[location_key])
        still_present_target_ids.update(item.entity_id for item in target_by_location[location_key])

    # Git can report a low-similarity move as one delete plus one add. Pair
    # those candidates globally so traversal order cannot claim a source for
    # the first of several same-signature destinations.
    deleted_by_key: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    added_by_key: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    all_base_by_key: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    all_target_by_key: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    for entity in base.entities:
        if entity.kind in {"Class", "Function", "Method"}:
            all_base_by_key[_symbol_signature(entity, base)].append(entity)
    for entity in target.entities:
        if entity.kind in {"Class", "Function", "Method"}:
            all_target_by_key[_symbol_signature(entity, target)].append(entity)
    for change in changed_files:
        if change.status == GitChangeStatus.DELETED:
            for entity in base.entities:
                if (
                    entity.kind in {"Class", "Function", "Method"}
                    and entity.file_path == change.old_path
                    and entity.entity_id not in still_present_base_ids
                    and _intersects(entity, change.base_ranges)
                ):
                    deleted_by_key[_symbol_signature(entity, base)].append(entity)
        elif change.status == GitChangeStatus.ADDED:
            for entity in target.entities:
                if (
                    entity.kind in {"Class", "Function", "Method"}
                    and entity.file_path == change.new_path
                    and entity.entity_id not in still_present_target_ids
                    and _intersects(entity, change.target_ranges)
                ):
                    added_by_key[_symbol_signature(entity, target)].append(entity)
    move_pairs_by_old: dict[str, tuple[Any, ChangedFile]] = {}
    move_pairs_by_new: dict[str, tuple[Any, ChangedFile]] = {}
    deleted_change_by_id = {
        entity.entity_id: change
        for change in changed_files
        if change.status == GitChangeStatus.DELETED
        for entity in base.entities
        if entity.file_path == change.old_path
    }
    for move_key in sorted(set(deleted_by_key) & set(added_by_key)):
        olds = deleted_by_key[move_key]
        news = added_by_key[move_key]
        if (
            len(olds) == len(news) == 1
            and len(all_base_by_key[move_key]) == 1
            and len(all_target_by_key[move_key]) == 1
        ):
            old, new = olds[0], news[0]
            move_pairs_by_old[old.entity_id] = (new, deleted_change_by_id[old.entity_id])
            move_pairs_by_new[new.entity_id] = (old, deleted_change_by_id[old.entity_id])

    claimed_base_ids: set[str] = set(unchanged_base_ids)
    claimed_target_ids: set[str] = set(unchanged_target_ids)
    emitted_move_old: set[str] = set()
    emitted_move_new: set[str] = set()

    def add(
        change: ChangedFile,
        classification: Literal["ADDED", "DELETED", "MODIFIED", "RENAMED_OR_MOVED", "UNMAPPED"],
        old: Any | None,
        new: Any | None,
        reason: str,
    ) -> None:
        evidence: list[DiffEvidence] = []
        if old is not None:
            evidence.append(_source_evidence(base, old))
        if new is not None:
            evidence.append(_source_evidence(target, new))
        if classification == "UNMAPPED":
            for analysis, path, ranges in (
                (base, change.old_path, change.base_ranges),
                (target, change.new_path, change.target_ranges),
            ):
                entry = next((item for item in analysis.manifest if item.path == path), None)
                evidence.append(
                    DiffEvidence(
                        evidence_type="SOURCE_RANGE",
                        snapshot_id=analysis.snapshot_id,
                        record_id=stable_id(
                            change.change_id,
                            analysis.snapshot_id,
                            path or "",
                            prefix="file_change_",
                        ),
                        file_path=path,
                        content_hash=entry.content_hash if entry else None,
                        start_line=(
                            ranges[0].start_line if ranges and ranges[0].line_count else None
                        ),
                    )
                )
        symbols.append(
            ChangedSymbol(
                change_id=change.change_id,
                classification=classification,
                base_snapshot_id=base.snapshot_id,
                target_snapshot_id=target.snapshot_id,
                base_entity_id=old.entity_id if old is not None else None,
                target_entity_id=new.entity_id if new is not None else None,
                base_qualified_name=old.qualified_name if old is not None else None,
                target_qualified_name=new.qualified_name if new is not None else None,
                base_file_path=old.file_path if old is not None else change.old_path,
                target_file_path=new.file_path if new is not None else change.new_path,
                deterministic_reason=reason,
                evidence=tuple(evidence),
            )
        )
        if old is not None:
            claimed_base_ids.add(old.entity_id)
        if new is not None:
            claimed_target_ids.add(new.entity_id)
        if len(symbols) > MAX_DIFF_SYMBOLS:
            raise M21DiffError("Changed symbols exceed the M21 limit of 10,000")

    for change in changed_files:
        has_claimed_old_candidate = any(
            item.kind in {"Class", "Function", "Method"}
            and item.file_path == change.old_path
            and item.entity_id in claimed_base_ids
            and (change.status == GitChangeStatus.ADDED or _intersects(item, change.base_ranges))
            for item in base.entities
        )
        has_claimed_new_candidate = any(
            item.kind in {"Class", "Function", "Method"}
            and item.file_path == change.new_path
            and item.entity_id in claimed_target_ids
            and (
                change.status == GitChangeStatus.DELETED or _intersects(item, change.target_ranges)
            )
            for item in target.entities
        )
        old_candidates = [
            item
            for item in base.entities
            if item.kind in {"Class", "Function", "Method"}
            and item.file_path == change.old_path
            and item.entity_id not in claimed_base_ids
            and (change.status == GitChangeStatus.ADDED or _intersects(item, change.base_ranges))
        ]
        new_candidates = [
            item
            for item in target.entities
            if item.kind in {"Class", "Function", "Method"}
            and item.file_path == change.new_path
            and item.entity_id not in claimed_target_ids
            and (
                change.status == GitChangeStatus.DELETED or _intersects(item, change.target_ranges)
            )
        ]
        if change.status == GitChangeStatus.MODIFIED:
            old_candidates = _most_specific_changed_symbols(old_candidates, change.base_ranges)
            new_candidates = _most_specific_changed_symbols(new_candidates, change.target_ranges)
        if change.status == GitChangeStatus.ADDED:
            old_candidates = []
            new_candidates = [
                item
                for item in target.entities
                if item.kind in {"Class", "Function", "Method"}
                and item.file_path == change.new_path
                and item.entity_id not in claimed_target_ids
            ]
        if change.status == GitChangeStatus.DELETED:
            old_candidates = [
                item
                for item in base.entities
                if item.kind in {"Class", "Function", "Method"}
                and item.file_path == change.old_path
                and item.entity_id not in claimed_base_ids
            ]
            new_candidates = []
        if change.status in {GitChangeStatus.RENAMED, GitChangeStatus.COPIED}:
            if not change.base_ranges:
                old_candidates = [
                    item
                    for item in base.entities
                    if item.kind in {"Class", "Function", "Method"}
                    and item.file_path == change.old_path
                    and item.entity_id not in claimed_base_ids
                ]
            if not change.target_ranges:
                new_candidates = [
                    item
                    for item in target.entities
                    if item.kind in {"Class", "Function", "Method"}
                    and item.file_path == change.new_path
                    and item.entity_id not in claimed_target_ids
                ]
        if change.status == GitChangeStatus.ADDED:
            new_candidates = _most_specific_changed_symbols(new_candidates, change.target_ranges)
        if change.status == GitChangeStatus.DELETED:
            old_candidates = _most_specific_changed_symbols(old_candidates, change.base_ranges)

        paired_old: set[str] = set()
        paired_new: set[str] = set()
        old_groups: dict[tuple[str, ...], list[Any]] = defaultdict(list)
        new_groups: dict[tuple[str, ...], list[Any]] = defaultdict(list)
        for item in old_candidates:
            old_groups[_symbol_signature(item, base)].append(item)
        for item in new_candidates:
            new_groups[_symbol_signature(item, target)].append(item)
        for signature_key in sorted(set(old_groups) | set(new_groups)):
            olds = sorted(
                old_groups.get(signature_key, ()),
                key=lambda item: (item.span.start_byte, item.entity_id),
            )
            news = sorted(
                new_groups.get(signature_key, ()),
                key=lambda item: (item.span.start_byte, item.entity_id),
            )
            if len(olds) > 1 or len(news) > 1:
                for item in olds:
                    paired_old.add(item.entity_id)
                    add(
                        change,
                        "UNMAPPED",
                        item,
                        None,
                        "Ambiguous same-signature symbol match; no pair was inferred.",
                    )
                for item in news:
                    paired_new.add(item.entity_id)
                    add(
                        change,
                        "UNMAPPED",
                        None,
                        item,
                        "Ambiguous same-signature symbol match; no pair was inferred.",
                    )
                continue
            old = olds[0] if olds else None
            new = news[0] if news else None
            if old is not None and new is not None:
                paired_old.add(old.entity_id)
                paired_new.add(new.entity_id)
                moved = old.file_path != new.file_path or change.status in {
                    GitChangeStatus.RENAMED,
                    GitChangeStatus.COPIED,
                }
                add(
                    change,
                    "RENAMED_OR_MOVED" if moved else "MODIFIED",
                    old,
                    new,
                    "Git rename detection and a unique structural symbol signature "
                    "identify the moved symbol."
                    if moved
                    else "The Git changed line range intersects this symbol in both "
                    "immutable snapshots.",
                )

        # A one-sided changed range can still identify the same logical symbol on
        # the other side (for example, an insertion at the end of a function).
        for old in old_candidates:
            if old.entity_id in paired_old:
                continue
            move_pair = move_pairs_by_old.get(old.entity_id)
            if move_pair is not None and old.entity_id not in emitted_move_old:
                new, _added_change = move_pair
                if new.entity_id in emitted_move_new:
                    move_pair = None
                else:
                    emitted_move_old.add(old.entity_id)
                    emitted_move_new.add(new.entity_id)
                    paired_old.add(old.entity_id)
                    paired_new.add(new.entity_id)
                    add(
                        change,
                        "RENAMED_OR_MOVED",
                        old,
                        new,
                        "A unique unmatched base symbol was deleted and a unique "
                        "same-signature target symbol was added in another file.",
                    )
                    continue
            if move_pair is None:
                paired_old.add(old.entity_id)
                old_classification: Literal["DELETED", "UNMAPPED"] = (
                    "DELETED" if change.status != GitChangeStatus.ADDED else "UNMAPPED"
                )
                add(
                    change,
                    old_classification,
                    old,
                    None,
                    "The changed source range maps to a symbol removed from the target snapshot."
                    if old_classification == "DELETED"
                    else "No unambiguous target symbol maps to this changed range.",
                )

        for new in new_candidates:
            if new.entity_id in paired_new:
                continue
            move_pair = move_pairs_by_new.get(new.entity_id)
            if move_pair is not None and new.entity_id not in emitted_move_new:
                old, deleted_change = move_pair
                if old.entity_id in emitted_move_old:
                    move_pair = None
                else:
                    emitted_move_old.add(old.entity_id)
                    emitted_move_new.add(new.entity_id)
                    paired_old.add(old.entity_id)
                    paired_new.add(new.entity_id)
                    add(
                        deleted_change,
                        "RENAMED_OR_MOVED",
                        old,
                        new,
                        "A unique unmatched base symbol was deleted and a unique "
                        "same-signature target symbol was added in another file.",
                    )
                    continue
            if move_pair is None:
                paired_new.add(new.entity_id)
                new_classification: Literal["ADDED", "UNMAPPED"] = (
                    "ADDED" if change.status != GitChangeStatus.DELETED else "UNMAPPED"
                )
                add(
                    change,
                    new_classification,
                    None,
                    new,
                    "The changed source range maps to a symbol added in the target snapshot."
                    if new_classification == "ADDED"
                    else "No unambiguous base symbol maps to this changed range.",
                )
        if (
            not old_candidates
            and not new_candidates
            and not has_claimed_old_candidate
            and not has_claimed_new_candidate
        ):
            add(
                change,
                "UNMAPPED",
                None,
                None,
                "Changed file ranges do not overlap a supported structural symbol "
                "in either snapshot.",
            )

    return sorted(
        symbols,
        key=lambda item: (
            item.change_id,
            item.base_file_path or item.target_file_path or "",
            item.base_qualified_name or item.target_qualified_name or "",
            item.classification,
            item.base_entity_id or "",
            item.target_entity_id or "",
        ),
    )


def _blob_loader(analysis: SnapshotAnalysis, settings: Settings) -> Callable[[str], bytes | None]:
    entries = {item.path: item for item in analysis.manifest if item.analyzed}
    root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id / "blobs"

    def load(path: str) -> bytes | None:
        entry = entries.get(path)
        if entry is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", entry.blob_hash):
            return None
        try:
            from .securefs import read_repository_file

            opened = read_repository_file(root, entry.blob_hash)
        except (OSError, ValueError):
            return None
        content = opened[0] if opened is not None else None
        if content is None:
            return None
        import hashlib

        if hashlib.sha256(content).hexdigest() != entry.content_hash:
            return None
        return content

    return load


def _path_mapped_test_files(source_path: str) -> set[str]:
    path = PurePosixPath(source_path)
    if path.suffix.lower() not in {".py", ".pyi"}:
        return set()
    parts = list(path.parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    tests: set[str] = set()
    for root in ("tests", "test"):
        tests.add(str(PurePosixPath(root, *parts[:-1], f"test_{path.stem}.py")))
        tests.add(str(PurePosixPath(root, f"test_{path.stem}.py")))
        tests.add(str(PurePosixPath(root, *parts[:-1], path.name)))
    return tests


def build_test_plan(
    target: SnapshotAnalysis,
    target_tools: DeterministicGraphTools,
    changed_symbols: Sequence[ChangedSymbol],
    impact_paths: Sequence[ImpactPath],
    *,
    test_definitions: Sequence[TestDefinition] = (),
    test_invocations: Sequence[Any] = (),
    collection_status: Literal["COMPLETED", "UNAVAILABLE", "FAILED"] = "COMPLETED",
    collection_error: str | None = None,
    collection_run_id: str | None = None,
    collection_environment_id: str | None = None,
    collection_materialization_id: str | None = None,
    source_loader: Callable[[str], bytes | None] | None = None,
) -> TestPlan:
    changed_ids = {
        item.target_entity_id
        for item in changed_symbols
        if item.target_entity_id and item.classification != "UNMAPPED"
    }
    impact_by_dependent = {
        item.dependent_entity_id: item
        for item in impact_paths
        if item.snapshot_id == target.snapshot_id
    }
    relevant_ids = changed_ids | set(impact_by_dependent)
    observed = (
        target_tools.find_observed_tests(
            GraphToolRequest(snapshot_id=target.snapshot_id, limit=MAX_RECOMMENDATIONS),
            entity_ids=sorted(relevant_ids),
        ).results
        if relevant_ids
        else ()
    )
    expected_materialization_id = target.runtime_materialization_id
    runtime_boundary_matches = bool(
        expected_materialization_id
        and collection_materialization_id == expected_materialization_id
        and collection_environment_id
    )

    def has_scoped_runtime_evidence(raw: Mapping[str, Any]) -> bool:
        evidence = raw.get("evidence")
        return bool(
            isinstance(evidence, Mapping)
            and evidence.get("snapshot_id") == target.snapshot_id
            and isinstance(evidence.get("evidence_id"), str)
            and evidence.get("evidence_id")
            and isinstance(evidence.get("source_record_id"), str)
            and evidence.get("source_record_id")
        )

    compatible_observations = [
        raw
        for raw in observed
        if runtime_boundary_matches
        and raw.get("runtime_materialization_id") == expected_materialization_id
        and raw.get("runtime_environment_id") == collection_environment_id
        and has_scoped_runtime_evidence(raw)
    ]
    baseline = bool(compatible_observations)

    invocations_by_definition: dict[str, list[Any]] = defaultdict(list)
    for invocation in test_invocations:
        if invocation.snapshot_id == target.snapshot_id:
            invocations_by_definition[invocation.test_definition_id].append(invocation)
    by_invocation_id = {
        invocation.test_invocation_id: invocation
        for values in invocations_by_definition.values()
        for invocation in values
    }

    recommendations: dict[str, TestRecommendation] = {}
    for raw in compatible_observations:
        invocation_id = str(raw.get("test_invocation_id") or "")
        invocation = by_invocation_id.get(invocation_id)
        node_id = invocation.node_id if invocation else str(raw.get("node_id") or "")
        if not node_id:
            continue
        ids = set(str(value) for value in raw.get("entity_ids", ()) if value)
        direct = ids & changed_ids
        dependent = ids & set(impact_by_dependent)
        raw_evidence = raw.get("evidence")
        if (
            not isinstance(raw_evidence, Mapping)
            or raw_evidence.get("snapshot_id") != target.snapshot_id
            or not isinstance(raw_evidence.get("evidence_id"), str)
            or not isinstance(raw_evidence.get("source_record_id"), str)
        ):
            continue
        evidence = [
            DiffEvidence(
                evidence_type="RUNTIME_OBSERVATION",
                snapshot_id=target.snapshot_id,
                record_id=str(raw_evidence["source_record_id"]),
                evidence_id=str(raw_evidence["evidence_id"]),
                runtime_materialization_id=expected_materialization_id,
                runtime_environment_id=collection_environment_id,
                run_id=str(raw.get("run_id") or "") or None,
            )
        ]
        if direct:
            recommendations[node_id] = TestRecommendation(
                test_definition_id=str(raw.get("test_definition_id") or "") or None,
                test_invocation_id=invocation_id or None,
                pytest_node_id=node_id,
                tier="A",
                label="RUNTIME_DIRECT",
                deterministic_reason=(
                    "This test invocation has a scoped runtime observation on a changed "
                    "target-snapshot symbol."
                ),
                evidence=tuple(evidence),
            )
        elif dependent:
            impact = min(
                (impact_by_dependent[item] for item in dependent),
                key=lambda item: (item.distance, item.uncertainty_count, item.path),
            )
            if not impact.evidence_complete:
                continue
            evidence.extend(impact.evidence)
            recommendations[node_id] = TestRecommendation(
                test_definition_id=str(raw.get("test_definition_id") or "") or None,
                test_invocation_id=invocation_id or None,
                pytest_node_id=node_id,
                tier="B",
                label="RUNTIME_DEPENDENT",
                deterministic_reason=(
                    "This test invocation observed a graph-dependent symbol connected "
                    "to a changed symbol "
                    f"by a {impact.distance}-hop {impact.mode} path."
                ),
                evidence=tuple(evidence),
                impact_path=impact,
            )

    mapped_test_files: set[str] = set()
    change_evidence_by_source: dict[str, list[DiffEvidence]] = defaultdict(list)
    change_evidence_by_name: dict[str, list[DiffEvidence]] = defaultdict(list)
    changed_names = {
        (item.target_qualified_name or item.base_qualified_name or "").rsplit(".", 1)[-1]
        for item in changed_symbols
        if item.classification != "UNMAPPED"
    }
    for item in changed_symbols:
        for source_path in sorted(
            value for value in (item.target_file_path, item.base_file_path) if value is not None
        ):
            mapped_test_files.update(_path_mapped_test_files(source_path))
            change_evidence_by_source[source_path].extend(item.evidence)
        symbol_name = (item.target_qualified_name or item.base_qualified_name or "").rsplit(".", 1)[
            -1
        ]
        if symbol_name and item.classification != "UNMAPPED":
            change_evidence_by_name[symbol_name].extend(item.evidence)
    read_source = source_loader or (lambda _path: None)
    collection_evidence = DiffEvidence(
        evidence_type="TEST_COLLECTION",
        snapshot_id=target.snapshot_id,
        record_id=collection_run_id
        or stable_id(
            target.snapshot_id,
            collection_materialization_id or "",
            collection_environment_id or "",
            "pytest-collect-only",
            prefix="collection_",
        ),
        runtime_materialization_id=collection_materialization_id,
        runtime_environment_id=collection_environment_id,
        run_id=collection_run_id,
    )
    for definition in test_definitions:
        if definition.snapshot_id != target.snapshot_id:
            continue
        content = read_source(definition.file_path) or b""
        static_refs = [
            name
            for name in changed_names
            if name
            and re.search(
                rb"(?<![A-Za-z0-9_])" + re.escape(name.encode("utf-8")) + rb"(?![A-Za-z0-9_])",
                content,
            )
        ]
        mapped = definition.file_path in mapped_test_files
        if not mapped and not static_refs:
            continue
        for invocation in sorted(
            invocations_by_definition.get(definition.test_definition_id, ()),
            key=lambda item: item.node_id,
        ):
            if invocation.node_id in recommendations:
                continue
            reasons = []
            if mapped:
                reasons.append("explicit source-to-test path mapping")
            if static_refs:
                reasons.append("static test-source reference to " + ", ".join(sorted(static_refs)))
            test_evidence = [collection_evidence]
            if mapped:
                for source_path, evidence_items in change_evidence_by_source.items():
                    if definition.file_path in _path_mapped_test_files(source_path):
                        test_evidence.extend(evidence_items)
            for name in static_refs:
                test_evidence.extend(change_evidence_by_name[name])
            recommendations[invocation.node_id] = TestRecommendation(
                test_definition_id=definition.test_definition_id,
                test_invocation_id=invocation.test_invocation_id,
                pytest_node_id=invocation.node_id,
                tier="C",
                label="COARSE_HEURISTIC",
                deterministic_reason="Tier C coarse safety-net selection by "
                + " and ".join(reasons)
                + ".",
                evidence=tuple(dict.fromkeys(test_evidence)),
            )

    tier_order = {"A": 0, "B": 1, "C": 2}
    ordered = sorted(
        recommendations.values(),
        key=lambda item: (tier_order[item.tier], item.pytest_node_id or ""),
    )[:MAX_RECOMMENDATIONS]
    diagnostics: list[str] = []
    if not baseline:
        diagnostics.append(
            "No runtime baseline is available. Coverage-backed Tier A/B "
            "recommendations are unavailable."
        )
        if observed and (
            not runtime_boundary_matches or len(compatible_observations) < len(observed)
        ):
            diagnostics.append(
                "Stored runtime observations do not match the target snapshot's exact "
                "runtime materialization and collected environment."
            )
    if collection_status == "UNAVAILABLE":
        diagnostics.append(
            collection_error
            or "Docker test collection is unavailable; Tier C pytest node IDs "
            "could not be collected."
        )
    elif collection_status == "FAILED":
        diagnostics.append(
            collection_error
            or "Isolated pytest collection failed; Tier C recommendations are unavailable."
        )
    if not any(item.tier in {"A", "B"} for item in ordered):
        diagnostics.append("No evidence-backed runtime test recommendation is available.")
    if not ordered and collection_status == "COMPLETED":
        diagnostics.append("No collected tests matched the deterministic Tier C mapping rules.")
    tiers = {
        "A": "AVAILABLE" if baseline else "UNAVAILABLE_NO_RUNTIME_BASELINE",
        "B": "AVAILABLE" if baseline else "UNAVAILABLE_NO_RUNTIME_BASELINE",
        "C": "AVAILABLE" if collection_status == "COMPLETED" else "UNAVAILABLE_COLLECTION",
    }
    return TestPlan(
        snapshot_id=target.snapshot_id,
        runtime_materialization_id=expected_materialization_id,
        runtime_environment_id=collection_environment_id,
        runtime_baseline_available=baseline,
        collection_status=collection_status,
        tiers=tiers,
        recommendations=tuple(ordered),
        diagnostics=tuple(diagnostics),
    )


def _materialize_git_tree(
    source_root: Path,
    destination: Path,
    commit_sha: str,
    *,
    max_blob_bytes: int = DEFAULT_M21_MAX_BLOB_BYTES,
    max_total_bytes: int = DEFAULT_M21_MAX_MATERIALIZED_BYTES,
    timeout_seconds: int = DEFAULT_M21_MATERIALIZATION_TIMEOUT_SECONDS,
) -> None:
    """Copy committed Git blobs without invoking checkout filters or hooks."""
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    entries = _git(
        source_root,
        "ls-tree",
        "-rz",
        "--full-tree",
        commit_sha,
        timeout=timeout_seconds,
    )
    records = [item for item in entries.split(b"\0") if item]
    if len(records) > 200_000:
        raise M21DiffError("Commit contains more than 200,000 tracked tree entries")
    parsed: list[tuple[int, str, str, str]] = []
    seen_paths: set[str] = set()
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode_text, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            relative = raw_path.decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise M21DiffError("Git returned an invalid tree entry") from exc
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise M21DiffError("Commit contains an unsafe repository path")
        if ".git" in path.parts:
            continue
        normalized_path = path.as_posix().casefold()
        if normalized_path in seen_paths:
            raise M21DiffError("Commit contains paths that collide on this filesystem")
        seen_paths.add(normalized_path)
        if object_type == "commit" or mode_text == "160000":
            # Submodule contents are separate repositories and are not part of this snapshot.
            continue
        if object_type != "blob":
            raise M21DiffError("Commit contains an unsupported Git tree object")
        try:
            mode = int(mode_text, 8)
        except ValueError as exc:
            raise M21DiffError("Git returned an invalid tree mode") from exc
        parsed.append((mode, object_type, object_id, relative))

    try:
        process = subprocess.Popen(
            ["git", "-C", str(source_root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise M21DiffError("Git object streaming could not start") from exc
    stdout = process.stdout
    if process.stdin is None or stdout is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                process.kill()
        process.wait()
        shutil.rmtree(destination, ignore_errors=True)
        raise M21DiffError("Git object streaming is unavailable")
    stdout_fd = stdout.fileno()
    total_streamed_bytes = 0
    blob_streamed_bytes = 0
    symlinks: list[tuple[Path, str, str]] = []

    def read_exact(
        size: int,
        output: Any | None = None,
        *,
        limit: int | None = None,
        count_as_blob: bool = False,
    ) -> bytes:
        nonlocal total_streamed_bytes, blob_streamed_bytes
        remaining = size
        collected = bytearray()
        while remaining:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise M21DiffError("Commit materialization exceeded its wall-clock deadline")
            try:
                readable, _, _ = select.select([stdout_fd], [], [], remaining_time)
            except (OSError, ValueError) as exc:
                raise M21DiffError("Git object stream became unavailable") from exc
            if not readable:
                raise M21DiffError("Commit materialization exceeded its wall-clock deadline")
            chunk = os.read(stdout_fd, min(64 * 1024, remaining))
            if not chunk:
                raise M21DiffError("Git returned a truncated blob")
            remaining -= len(chunk)
            if count_as_blob:
                blob_streamed_bytes += len(chunk)
                total_streamed_bytes += len(chunk)
                if blob_streamed_bytes > max_blob_bytes:
                    raise M21DiffError("Commit blob exceeds the configured per-file limit")
                if total_streamed_bytes > max_total_bytes:
                    raise M21DiffError(
                        "Commit materialization exceeds the configured total-byte limit"
                    )
            if output is not None:
                output.write(chunk)
            elif limit is not None and len(collected) + len(chunk) > limit:
                raise M21DiffError("Git symlink target exceeds the safe path limit")
            else:
                collected.extend(chunk)
        return bytes(collected)

    def read_line(max_bytes: int = 256) -> bytes:
        result = bytearray()
        while len(result) < max_bytes:
            char = read_exact(1)
            if char == b"\n":
                return bytes(result)
            result.extend(char)
        raise M21DiffError("Git returned an oversized object header")

    def read_newline() -> None:
        if read_exact(1) != b"\n":
            raise M21DiffError("Git returned an invalid blob record")

    try:
        for mode, _object_type, object_id, relative in parsed:
            if time.monotonic() >= deadline:
                raise M21DiffError("Commit materialization exceeded its wall-clock deadline")
            path = PurePosixPath(relative)
            if any(part in {"", ".", ".."} for part in path.parts):
                raise M21DiffError("Commit contains an unsafe repository path")
            output_path = destination.joinpath(*path.parts)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            current = output_path.parent
            while current != destination:
                if current.is_symlink():
                    raise M21DiffError("Commit path traverses a symlink parent")
                current = current.parent
            if output_path.exists() or output_path.is_symlink():
                raise M21DiffError("Commit contains conflicting paths on this filesystem")
            process.stdin.write((object_id + "\n").encode("ascii"))
            process.stdin.flush()
            header = read_line().decode("ascii", "replace").strip().split()
            if len(header) != 3 or header[0] != object_id or header[1] != "blob":
                raise M21DiffError("Git returned an unexpected blob response")
            try:
                size = int(header[2])
            except ValueError as exc:
                raise M21DiffError("Git returned an invalid blob size") from exc
            if size < 0 or size > max_blob_bytes:
                raise M21DiffError("Commit blob exceeds the configured per-file limit")
            if total_streamed_bytes + size > max_total_bytes:
                raise M21DiffError("Commit materialization exceeds the configured total-byte limit")
            blob_streamed_bytes = 0
            if mode == 0o120000:
                target_bytes = read_exact(size, limit=4096, count_as_blob=True)
                read_newline()
                try:
                    link_target = target_bytes.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise M21DiffError(
                        "Commit symlink targets must be valid UTF-8 for deterministic analysis"
                    ) from exc
                target_path = PurePosixPath(link_target)
                resolved = PurePosixPath(
                    posixpath.normpath(posixpath.join(path.parent.as_posix(), link_target))
                )
                if target_path.is_absolute() or ".." in resolved.parts or ".git" in resolved.parts:
                    raise M21DiffError("Commit contains a symlink that escapes the snapshot")
                symlinks.append((output_path, link_target, relative))
                continue
            if mode not in {0o100644, 0o100755}:
                raise M21DiffError("Commit contains an unsupported file mode")
            with output_path.open("xb") as file:
                read_exact(size, file, count_as_blob=True)
            read_newline()
            output_path.chmod(stat.S_IMODE(mode))
        process.stdin.close()
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise M21DiffError("Commit materialization exceeded its wall-clock deadline")
        try:
            process_status = process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired as exc:
            raise M21DiffError("Commit materialization exceeded its wall-clock deadline") from exc
        if process_status != 0:
            raise M21DiffError("Git could not read all committed blobs")
        for output_path, link_target, _relative in symlinks:
            if output_path.exists() or output_path.is_symlink():
                raise M21DiffError("Commit contains conflicting file and symlink paths")
            os.symlink(link_target, output_path)
        snapshot_root = destination.resolve(strict=True)
        for output_path, _link_target, _relative in symlinks:
            try:
                resolved_path = output_path.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise M21DiffError("Commit contains a cyclic or unsafe symlink") from exc
            if resolved_path != snapshot_root and snapshot_root not in resolved_path.parents:
                raise M21DiffError("Commit contains a symlink that escapes the snapshot")
    except BaseException as exc:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, OSError):
            raise M21DiffError(
                "Commit materialization failed while writing temporary files"
            ) from exc
        raise
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


class CommitDiffService:
    """M21 commit diff orchestration using immutable READY snapshots and existing safety gates."""

    def __init__(
        self,
        settings: Settings,
        jobs: IngestionJobManager,
        *,
        store_factory: Callable[[Settings], Any] = GraphStore,
        collection_provider: Callable[[SnapshotAnalysis], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.jobs = jobs
        self.store_factory = store_factory
        self.collection_provider = collection_provider

    @contextmanager
    def _commit_tree(self, root: Path, repository_id: str, remote: str | None, sha: str):
        """Materialize exact Git blobs without checkout hooks, filters, or repository code."""
        cache_parent = root / ".codegraph-cache"
        if cache_parent.is_symlink():
            resolved_cache_parent = cache_parent.resolve(strict=True)
            if resolved_cache_parent != root and root not in resolved_cache_parent.parents:
                raise M21DiffError("Temporary commit cache escapes the approved repository root")
        cache_parent.mkdir(parents=True, exist_ok=True)
        parent = cache_parent / "m21-commits"
        parent.mkdir(parents=True, exist_ok=True)
        safe_parent = parent.resolve(strict=True)
        if safe_parent != root and root not in safe_parent.parents:
            raise M21DiffError("Temporary commit path escapes the approved repository root")
        path = safe_parent / f"{sha[:12]}-{uuid.uuid4().hex}"
        path.mkdir(mode=0o700)
        hooks_dir = safe_parent / f"hooks-{uuid.uuid4().hex}"
        template_dir = safe_parent / f"template-{uuid.uuid4().hex}"
        try:
            hooks_dir.mkdir(mode=0o700)
            template_dir.mkdir(mode=0o700)
            _materialize_git_tree(
                root,
                path,
                sha,
                max_blob_bytes=self.settings.m21_max_commit_blob_bytes,
                max_total_bytes=self.settings.m21_max_commit_materialized_bytes,
                timeout_seconds=self.settings.m21_commit_materialization_timeout_seconds,
            )
            _git(root, "init", "--quiet", f"--template={template_dir}", str(path))
            _git(path, "config", "--local", "core.hooksPath", str(hooks_dir))
            _git(path, "config", "--local", "core.fsmonitor", "false")
            if remote:
                _git(path, "remote", "add", "origin", remote)
            common_dir_raw = _git(root, "rev-parse", "--git-common-dir").decode().strip()
            common_dir = Path(common_dir_raw)
            if not common_dir.is_absolute():
                common_dir = root / common_dir
            object_dir = (common_dir.resolve(strict=True) / "objects").resolve(strict=True)
            alternates = path / ".git" / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(str(object_dir) + "\n", encoding="utf-8")
            if _git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
                shallow_path_raw = _git(root, "rev-parse", "--git-path", "shallow").decode().strip()
                shallow_path = Path(shallow_path_raw)
                if not shallow_path.is_absolute():
                    shallow_path = root / shallow_path
                if shallow_path.is_file():
                    (path / ".git" / "shallow").write_bytes(shallow_path.read_bytes())
            _git(path, "update-ref", "refs/heads/m21-snapshot", sha)
            _git(path, "symbolic-ref", "HEAD", "refs/heads/m21-snapshot")
            _git(path, "read-tree", sha)
            yield path
        finally:
            try:
                if path.is_symlink():
                    path.unlink()
                elif path.exists() and path.resolve() != root and root in path.resolve().parents:
                    shutil.rmtree(path)
                shutil.rmtree(hooks_dir, ignore_errors=True)
                shutil.rmtree(template_dir, ignore_errors=True)
            except OSError as exc:
                raise M21DiffError(
                    "Could not clean up the temporary commit materialization"
                ) from exc

    def _snapshot(
        self, worktree: Path, repository: RepositoryInfo, expected_sha: str
    ) -> SnapshotAnalysis:
        snapshot_id, snapshot_repository = identify_snapshot(
            worktree,
            self.settings,
            repository_id_override=repository.repository_id,
            skip_worktree_status=True,
        )
        if snapshot_repository.head_commit != expected_sha:
            raise M21DiffError("Commit worktree does not match the requested immutable commit")
        register_active_snapshot(snapshot_id)
        try:
            store = self.store_factory(self.settings)
            if store.snapshot_status(snapshot_id) != "READY":
                self.jobs.submit(
                    worktree,
                    self.settings,
                    canonical_repository_root=repository.root,
                    repository_id_override=repository.repository_id,
                    skip_worktree_status=True,
                )
                completed = self.jobs.wait(snapshot_id)
                if completed.status != IngestionJobStatus.COMPLETED:
                    raise M21DiffError(
                        "Required "
                        f"{expected_sha[:12]} snapshot ingestion did not complete "
                        f"({completed.status})"
                    )
            analysis = load_snapshot_analysis(snapshot_id, self.settings)
            if (
                analysis.snapshot_id != snapshot_id
                or analysis.repository.head_commit != expected_sha
            ):
                raise M21DiffError("Ingested snapshot identity does not match the requested commit")
            if store.snapshot_status(snapshot_id) != "READY":
                raise M21DiffError("Required commit snapshot is not READY")
            return analysis.model_copy(update={"status": SnapshotStatus.READY})
        except Exception:
            unregister_active_snapshot(snapshot_id)
            raise

    def analyze(self, request: DiffAnalysisRequest) -> DiffAnalysisResponse:
        root, repository = inspect_repository(
            request.repository_path, self.settings, skip_worktree_status=True
        )
        base_sha = _resolve_commit(root, request.base_sha)
        target_sha = _resolve_commit(root, request.target_sha)
        with ExitStack() as stack:
            with self._commit_tree(
                root, repository.repository_id, repository.remote, base_sha
            ) as base_worktree:
                base = self._snapshot(base_worktree, repository, base_sha)
                stack.callback(unregister_active_snapshot, base.snapshot_id)
                if target_sha == base_sha:
                    target = base
                else:
                    with self._commit_tree(
                        root, repository.repository_id, repository.remote, target_sha
                    ) as target_worktree:
                        target = self._snapshot(target_worktree, repository, target_sha)
                        stack.callback(unregister_active_snapshot, target.snapshot_id)
                        result = self._build_result(
                            root,
                            repository.repository_id,
                            base_sha,
                            target_sha,
                            base,
                            target,
                            request,
                        )
                        return result
                result = self._build_result(
                    root, repository.repository_id, base_sha, target_sha, base, target, request
                )
                return result
            # Snapshot activity guards are held until analysis and test collection finish.

    def _build_result(
        self,
        root: Path,
        repository_id: str,
        base_sha: str,
        target_sha: str,
        base: SnapshotAnalysis,
        target: SnapshotAnalysis,
        request: DiffAnalysisRequest,
    ) -> DiffAnalysisResponse:
        try:
            paths = _parse_changed_files(root, repository_id, base_sha, target_sha)
            paths = [_with_ranges(root, base_sha, target_sha, item) for item in paths]
            store = self.store_factory(self.settings)
            base_tools = DeterministicGraphTools.from_store(base, store)
            target_tools = DeterministicGraphTools.from_store(target, store)
            base_loader = _blob_loader(base, self.settings)
            target_loader = _blob_loader(target, self.settings)
            all_changed_symbols = map_changed_symbols(
                repository_id,
                base_sha,
                target_sha,
                base,
                target,
                paths,
                base_source_loader=base_loader,
                target_source_loader=target_loader,
            )
            changed_symbols = all_changed_symbols[: request.result_limit]
            impacts, impact_truncated = _impact_paths(
                changed_symbols,
                base,
                target,
                base_tools,
                target_tools,
                request.hop_limit,
                request.result_limit,
            )
            collection = None
            collection_status: Literal["COMPLETED", "UNAVAILABLE", "FAILED"] = "UNAVAILABLE"
            collection_error = None
            if self.collection_provider is not None:
                try:
                    collection = self.collection_provider(target)
                    collection_status = "COMPLETED"
                except Exception as exc:
                    from .test_execution import DockerUnavailable

                    collection_status = (
                        "UNAVAILABLE" if isinstance(exc, DockerUnavailable) else "FAILED"
                    )
                    collection_error = str(exc)[:500]
            test_plan = build_test_plan(
                target,
                target_tools,
                changed_symbols,
                impacts,
                test_definitions=getattr(collection, "test_definitions", ()),
                test_invocations=getattr(collection, "test_invocations", ()),
                collection_status=collection_status,
                collection_error=collection_error,
                collection_run_id=getattr(
                    getattr(collection, "collection_result", None), "run_id", None
                ),
                collection_environment_id=getattr(
                    getattr(collection, "runtime_environment", None),
                    "runtime_environment_id",
                    None,
                ),
                collection_materialization_id=getattr(
                    getattr(collection, "runtime_environment", None),
                    "runtime_materialization_id",
                    None,
                ),
                source_loader=_blob_loader(target, self.settings),
            )
            analysis_truncated = len(all_changed_symbols) > len(changed_symbols)
            diagnostics = [
                "Changed symbol results reached the requested deterministic result limit."
                if analysis_truncated
                else ""
            ]
            diagnostics = [item for item in diagnostics if item]
            if impact_truncated or len(impacts) > request.result_limit:
                diagnostics.append(
                    "Impact results reached a deterministic candidate or result bound."
                )
            return DiffAnalysisResponse(
                repository_id=repository_id,
                base_commit_sha=base_sha,
                target_commit_sha=target_sha,
                base_snapshot_id=base.snapshot_id,
                target_snapshot_id=target.snapshot_id,
                changed_files=tuple(paths),
                changed_symbols=tuple(changed_symbols),
                impact_paths=tuple(impacts[: request.result_limit]),
                test_plan=test_plan,
                candidate_count_before_limit=len(all_changed_symbols),
                truncated=analysis_truncated
                or impact_truncated
                or len(impacts) > request.result_limit,
                diagnostics=(
                    *diagnostics,
                    *(
                        ("Base and target commits are identical; the diff is empty.",)
                        if base_sha == target_sha
                        else ()
                    ),
                    *(
                        ("Impact results reached the requested deterministic result limit.",)
                        if len(impacts) > request.result_limit
                        else ()
                    ),
                ),
            )
        except M21DiffError:
            raise
        except (RepositoryError, OSError, ValueError, KeyError) as exc:
            raise M21DiffError(str(exc)[:500]) from exc


def _impact_paths(
    symbols: Sequence[ChangedSymbol],
    base: SnapshotAnalysis,
    target: SnapshotAnalysis,
    base_tools: DeterministicGraphTools,
    target_tools: DeterministicGraphTools,
    hop_limit: int,
    result_limit: int,
) -> tuple[list[ImpactPath], bool]:
    impacts: dict[tuple[str, str, tuple[str, ...]], ImpactPath] = {}
    truncated = len(symbols) > MAX_IMPACT_ANCHORS
    for symbol in symbols[:MAX_IMPACT_ANCHORS]:
        selected: list[tuple[SnapshotAnalysis, DeterministicGraphTools, str | None]] = []
        if symbol.target_entity_id:
            selected.append((target, target_tools, symbol.target_entity_id))
        if symbol.base_entity_id and symbol.classification in {"DELETED", "RENAMED_OR_MOVED"}:
            selected.append((base, base_tools, symbol.base_entity_id))
        for analysis, tools, entity_id in selected:
            if not entity_id:
                continue
            provenance_by_id = {
                record.provenance_id: record
                for record in analysis.provenance
                if record.snapshot_id == analysis.snapshot_id
            }
            for mode in ("STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"):
                result = tools.find_impact(
                    ImpactToolRequest(
                        snapshot_id=analysis.snapshot_id,
                        target_id=entity_id,
                        mode=mode,
                        hop_limit=hop_limit,
                        limit=result_limit,
                    )
                )
                truncated = truncated or result.truncated
                for raw in result.results:
                    if len(impacts) >= MAX_TOTAL_IMPACT_PATHS:
                        truncated = True
                        break
                    steps = tuple(raw.get("steps") or ())
                    path_evidence: list[DiffEvidence] = []
                    evidence_complete = bool(steps)
                    for step in steps:
                        step_evidence = step.get("evidence")
                        if not isinstance(step_evidence, Sequence) or isinstance(
                            step_evidence, (str, bytes)
                        ):
                            evidence_complete = False
                            continue
                        valid_for_step = False
                        for raw_evidence in step_evidence:
                            if not isinstance(raw_evidence, Mapping):
                                continue
                            evidence_id = raw_evidence.get("evidence_id")
                            source_record_id = raw_evidence.get("source_record_id")
                            if not isinstance(evidence_id, str) or not isinstance(
                                source_record_id, str
                            ):
                                continue
                            valid_for_step = True
                            provenance_pointer = raw_evidence.get("provenance_pointer")
                            provenance = (
                                provenance_by_id.get(provenance_pointer)
                                if isinstance(provenance_pointer, str)
                                else None
                            )
                            if provenance is not None and provenance.edge_id != source_record_id:
                                provenance = None
                            path_evidence.append(
                                DiffEvidence(
                                    evidence_type=(
                                        "CONSERVATIVE_GRAPH_PATH"
                                        if step.get("uncertain")
                                        else "VERIFIED_GRAPH_PATH"
                                    ),
                                    snapshot_id=analysis.snapshot_id,
                                    record_id=source_record_id,
                                    evidence_id=evidence_id,
                                    file_path=provenance.file_path if provenance else None,
                                    content_hash=(
                                        provenance.file_content_hash if provenance else None
                                    ),
                                    start_line=(provenance.span.start_line if provenance else None),
                                    end_line=provenance.span.end_line if provenance else None,
                                    entity_id=str(step.get("source_id") or "") or None,
                                )
                            )
                        evidence_complete = evidence_complete and valid_for_step
                    uncertain = int(raw.get("uncertainty_count") or 0) > 0 or any(
                        bool(step.get("uncertain")) for step in steps
                    )
                    semantics = (
                        "INCOMPLETE"
                        if not evidence_complete
                        else "CONSERVATIVE"
                        if uncertain
                        else "STRICT_EXACT"
                    )
                    item = ImpactPath(
                        change_id=symbol.change_id,
                        snapshot_id=analysis.snapshot_id,
                        changed_entity_id=entity_id,
                        dependent_entity_id=str(raw["node_id"]),
                        distance=int(raw["distance"]),
                        mode=str(raw["mode"]),
                        path_semantics=semantics,
                        evidence_complete=evidence_complete,
                        uncertainty_count=int(raw["uncertainty_count"]),
                        path=tuple(str(value) for value in raw["path"]),
                        steps=steps,
                        evidence=tuple(path_evidence),
                        unresolved_barriers=tuple(raw.get("unresolved_barriers", ())),
                    )
                    key = (item.snapshot_id, item.dependent_entity_id, item.path)
                    impacts.setdefault(key, item)
            if len(impacts) >= MAX_TOTAL_IMPACT_PATHS:
                truncated = True
                break
        if len(impacts) >= MAX_TOTAL_IMPACT_PATHS:
            truncated = True
            break
    ordered = sorted(
        impacts.values(),
        key=lambda item: (
            item.snapshot_id,
            item.distance,
            item.uncertainty_count,
            item.path,
            item.path_semantics,
            item.change_id,
        ),
    )
    truncated = truncated or len(ordered) > result_limit
    return ordered[:result_limit], truncated

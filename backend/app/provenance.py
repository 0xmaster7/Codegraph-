from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

from .config import Settings
from .dispatch import derive_dispatch
from .environment import capture_environment
from .imports import build_import_relationships
from .ingestion import _parse_file
from .models import (
    FAIL_CLOSED_REPLAY_OUTCOMES,
    ReplayOutcome,
    ReplayResult,
    ReplayScope,
    ReviewState,
    SnapshotAnalysis,
)
from .scip import join_bindings, run_scip_index, scip_toolchain


def failed_closed_edge_ids(analysis: SnapshotAnalysis) -> set[str]:
    """Return edge IDs whose scoped replay found a deterministic/integrity mismatch."""
    return {
        record.edge_id
        for record in analysis.provenance
        if record.replay_state in FAIL_CLOSED_REPLAY_OUTCOMES
    }


def replay_edge(
    analysis: SnapshotAnalysis,
    evidence_id: str,
    settings: Settings,
) -> ReplayResult:
    record = next(
        (
            item
            for item in analysis.provenance
            if item.edge_id == evidence_id or item.provenance_id == evidence_id
        ),
        None,
    )
    edge_id = record.edge_id if record else evidence_id
    if record is None:
        return _result(
            edge_id,
            ReplayScope.FILE_OR_SPAN,
            ReplayOutcome.ERROR,
            message="Replay could not start: evidence identity was not found in this snapshot.",
        )

    sources: dict[str, bytes] = {}
    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    manifest = {entry.path: entry for entry in analysis.manifest}
    for entry in analysis.manifest:
        if not entry.analyzed:
            continue
        if Path(entry.path).is_absolute() or ".." in Path(entry.path).parts:
            return _result(
                edge_id, _scope(record.extractor_name, record.rule_id), ReplayOutcome.ERROR
            )
        blob = cache_root / "blobs" / entry.blob_hash
        try:
            content = blob.read_bytes()
        except OSError as exc:
            return _result(
                edge_id,
                _scope(record.extractor_name, record.rule_id),
                ReplayOutcome.ERROR,
                message=f"Snapshot source is unavailable ({type(exc).__name__}).",
            )
        if hashlib.sha256(content).hexdigest() != entry.content_hash:
            return _result(
                edge_id,
                _scope(record.extractor_name, record.rule_id),
                ReplayOutcome.SOURCE_HASH_MISMATCH,
            )
        sources[entry.path] = content

    provenance_entry = manifest.get(record.file_path)
    if provenance_entry is None or record.file_path not in sources:
        return _result(edge_id, _scope(record.extractor_name, record.rule_id), ReplayOutcome.ERROR)
    if provenance_entry.content_hash != record.file_content_hash:
        return _result(
            edge_id,
            _scope(record.extractor_name, record.rule_id),
            ReplayOutcome.SOURCE_HASH_MISMATCH,
        )

    if record.rule_id.startswith("imports."):
        external_symbols = analysis.external_symbols
        if record.extractor_name == "scip-python":
            if analysis.analysis_environment is None:
                return _result(edge_id, ReplayScope.SNAPSHOT_SEMANTIC, ReplayOutcome.ERROR)
            _, _, python_version, cli_version = scip_toolchain(settings)
            current_environment = capture_environment(
                analysis.analysis_environment.source_manifest_hash,
                python_version,
                cli_version,
            )
            if current_environment.environment_id != analysis.analysis_environment.environment_id:
                return _result(
                    edge_id,
                    ReplayScope.SNAPSHOT_SEMANTIC,
                    ReplayOutcome.ENVIRONMENT_MISMATCH,
                    current_environment.environment_id,
                )
            index, version, diagnostics = run_scip_index(analysis, settings)
            if index is None or version is None:
                message = (
                    diagnostics[0].message
                    if diagnostics
                    else "SCIP replay did not produce an index."
                )
                return _result(
                    edge_id,
                    ReplayScope.SNAPSHOT_SEMANTIC,
                    ReplayOutcome.ERROR,
                    message=message,
                )
            if version != record.extractor_version:
                return _result(
                    edge_id,
                    ReplayScope.SNAPSHOT_SEMANTIC,
                    ReplayOutcome.EXTRACTOR_VERSION_MISMATCH,
                )
            _, external_symbols, _, _ = join_bindings(
                analysis,
                sources,
                index,
                environment_id=analysis.analysis_environment.environment_id,
                extractor_version=version,
            )
        elif record.extractor_version != "1":
            return _result(
                edge_id,
                ReplayScope.FILE_OR_SPAN,
                ReplayOutcome.EXTRACTOR_VERSION_MISMATCH,
            )
        import_edges, _, _ = build_import_relationships(analysis, sources, external_symbols)
        outcome = (
            ReplayOutcome.PASS
            if any(item.edge_id == edge_id for item in import_edges)
            else ReplayOutcome.EDGE_NOT_REPRODUCED
        )
        return _result(edge_id, ReplayScope.FILE_OR_SPAN, outcome)

    if record.extractor_name == "scip-python":
        if analysis.analysis_environment is None:
            return _result(edge_id, ReplayScope.SNAPSHOT_SEMANTIC, ReplayOutcome.ERROR)
        _, _, python_version, cli_version = scip_toolchain(settings)
        current_environment = capture_environment(
            analysis.analysis_environment.source_manifest_hash,
            python_version,
            cli_version,
        )
        if current_environment.environment_id != analysis.analysis_environment.environment_id:
            return _result(
                edge_id,
                ReplayScope.SNAPSHOT_SEMANTIC,
                ReplayOutcome.ENVIRONMENT_MISMATCH,
                current_environment.environment_id,
            )
        index, version, diagnostics = run_scip_index(analysis, settings)
        if index is None or version is None:
            message = (
                diagnostics[0].message if diagnostics else "SCIP replay did not produce an index."
            )
            return _result(
                edge_id,
                ReplayScope.SNAPSHOT_SEMANTIC,
                ReplayOutcome.ERROR,
                message=message,
            )
        if version != record.extractor_version:
            return _result(
                edge_id, ReplayScope.SNAPSHOT_SEMANTIC, ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
            )
        bindings, _, _, _ = join_bindings(
            analysis,
            sources,
            index,
            environment_id=analysis.analysis_environment.environment_id,
            extractor_version=version,
        )
        outcome = (
            ReplayOutcome.PASS
            if any(binding.edge_id == edge_id for binding in bindings)
            else ReplayOutcome.EDGE_NOT_REPRODUCED
        )
        return _result(
            edge_id,
            ReplayScope.SNAPSHOT_SEMANTIC,
            outcome,
            current_environment.environment_id,
        )

    if record.extractor_name == "static-dispatch":
        scope = ReplayScope.RULE_PREMISES
        if record.extractor_version != "1":
            return _result(edge_id, scope, ReplayOutcome.EXTRACTOR_VERSION_MISMATCH)
        structural_edges = {
            item.edge_id for item in analysis.provenance if item.extractor_name != "static-dispatch"
        }
        semantic_edges = {item.edge_id for item in analysis.bindings}
        derived_edges = {item.edge_id for item in analysis.derived_edges}
        if not set(record.premise_edge_ids) <= structural_edges | semantic_edges | derived_edges:
            return _result(edge_id, scope, ReplayOutcome.PREMISE_MISSING)
        edges, _, _, _ = derive_dispatch(analysis, sources)
        regenerated = next((item for item in edges if item.edge_id == edge_id), None)
        outcome = (
            ReplayOutcome.PASS
            if regenerated is not None and regenerated.premise_edge_ids == record.premise_edge_ids
            else ReplayOutcome.EDGE_NOT_REPRODUCED
        )
        return _result(edge_id, scope, outcome)

    try:
        current_version = importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        return _result(
            edge_id,
            ReplayScope.FILE_OR_SPAN,
            ReplayOutcome.ERROR,
            message="Parser replay is unavailable: tree-sitter-language-pack is not installed.",
        )
    if current_version != record.extractor_version:
        return _result(edge_id, ReplayScope.FILE_OR_SPAN, ReplayOutcome.EXTRACTOR_VERSION_MISMATCH)
    _, _, provenance, _ = _parse_file(
        analysis.snapshot_id, record.file_path, sources[record.file_path], current_version
    )
    outcome = (
        ReplayOutcome.PASS
        if any(item.edge_id == edge_id for item in provenance)
        else ReplayOutcome.EDGE_NOT_REPRODUCED
    )
    return _result(edge_id, ReplayScope.FILE_OR_SPAN, outcome)


def replay_snapshot(
    analysis: SnapshotAnalysis,
    settings: Settings,
) -> list[ReplayResult]:
    """Replay every provenance record while running semantic and derivation passes once."""
    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    manifest = {entry.path: entry for entry in analysis.manifest}
    sources: dict[str, bytes] = {}
    source_failures: dict[str, ReplayOutcome] = {}
    for entry in analysis.manifest:
        if not entry.analyzed:
            continue
        if Path(entry.path).is_absolute() or ".." in Path(entry.path).parts:
            source_failures[entry.path] = ReplayOutcome.ERROR
            continue
        try:
            content = (cache_root / "blobs" / entry.blob_hash).read_bytes()
        except OSError:
            source_failures[entry.path] = ReplayOutcome.ERROR
            continue
        if hashlib.sha256(content).hexdigest() != entry.content_hash:
            source_failures[entry.path] = ReplayOutcome.SOURCE_HASH_MISMATCH
            continue
        sources[entry.path] = content

    current_parser_version: str | None
    try:
        current_parser_version = importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        current_parser_version = None
    parser_edges: dict[str, set[str]] = {}
    parser_failures: dict[str, ReplayOutcome] = {}
    for path, content in sources.items():
        if current_parser_version is None:
            parser_failures[path] = ReplayOutcome.ERROR
        else:
            try:
                _, _, records, _ = _parse_file(
                    analysis.snapshot_id, path, content, current_parser_version
                )
                parser_edges[path] = {item.edge_id for item in records}
            except Exception:
                parser_failures[path] = ReplayOutcome.ERROR

    semantic_edges: set[str] = set()
    semantic_external_symbols = analysis.external_symbols
    semantic_environment_id: str | None = None
    semantic_failure = ReplayOutcome.ERROR
    semantic_records = [
        item for item in analysis.provenance if item.extractor_name == "scip-python"
    ]
    if semantic_records and analysis.analysis_environment is not None:
        _, _, python_version, cli_version = scip_toolchain(settings)
        environment = capture_environment(
            analysis.analysis_environment.source_manifest_hash, python_version, cli_version
        )
        semantic_environment_id = environment.environment_id
        if environment.environment_id != analysis.analysis_environment.environment_id:
            semantic_failure = ReplayOutcome.ENVIRONMENT_MISMATCH
        else:
            index, version, _ = run_scip_index(analysis, settings)
            if index is not None and version is not None:
                if version != analysis.analysis_environment.scip_python_version:
                    semantic_failure = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
                else:
                    bindings, semantic_external_symbols, _, _ = join_bindings(
                        analysis,
                        sources,
                        index,
                        environment_id=environment.environment_id,
                        extractor_version=version,
                    )
                    semantic_edges = {item.edge_id for item in bindings}
                    semantic_failure = ReplayOutcome.PASS

    derived_edges: dict[str, list[str]] = {}
    derived_records = [
        item for item in analysis.provenance if item.extractor_name == "static-dispatch"
    ]
    if derived_records:
        if all(item.extractor_version == "1" for item in derived_records):
            regenerated, _, _, _ = derive_dispatch(analysis, sources)
            derived_edges = {item.edge_id: item.premise_edge_ids for item in regenerated}

    import_edges: set[str] = set()
    if any(item.rule_id.startswith("imports.") for item in analysis.provenance):
        import_external_symbols = (
            semantic_external_symbols
            if semantic_failure == ReplayOutcome.PASS
            else analysis.external_symbols
        )
        replayed_imports, _, _ = build_import_relationships(
            analysis, sources, import_external_symbols
        )
        import_edges = {item.edge_id for item in replayed_imports}

    results: list[ReplayResult] = []
    for record in analysis.provenance:
        scope = _scope(record.extractor_name, record.rule_id)
        provenance_entry = manifest.get(record.file_path)
        outcome: ReplayOutcome
        if record.file_path in source_failures:
            outcome = source_failures[record.file_path]
        elif provenance_entry is None or record.file_path not in sources:
            outcome = ReplayOutcome.ERROR
        elif provenance_entry.content_hash != record.file_content_hash:
            outcome = ReplayOutcome.SOURCE_HASH_MISMATCH
        elif record.rule_id.startswith("imports."):
            if record.extractor_name == "scip-python":
                if analysis.analysis_environment is None:
                    outcome = ReplayOutcome.ERROR
                elif record.extractor_version != analysis.analysis_environment.scip_python_version:
                    outcome = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
                elif semantic_failure != ReplayOutcome.PASS:
                    outcome = semantic_failure
                else:
                    outcome = (
                        ReplayOutcome.PASS
                        if record.edge_id in import_edges
                        else ReplayOutcome.EDGE_NOT_REPRODUCED
                    )
            elif record.extractor_version != "1":
                outcome = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
            else:
                outcome = (
                    ReplayOutcome.PASS
                    if record.edge_id in import_edges
                    else ReplayOutcome.EDGE_NOT_REPRODUCED
                )
        elif record.extractor_name == "scip-python":
            if analysis.analysis_environment is None:
                outcome = ReplayOutcome.ERROR
            elif record.extractor_version != analysis.analysis_environment.scip_python_version:
                outcome = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
            elif semantic_failure != ReplayOutcome.PASS:
                outcome = semantic_failure
            else:
                outcome = (
                    ReplayOutcome.PASS
                    if record.edge_id in semantic_edges
                    else ReplayOutcome.EDGE_NOT_REPRODUCED
                )
        elif record.extractor_name == "static-dispatch":
            known_premises = {
                item.edge_id
                for item in analysis.provenance
                if item.extractor_name != "static-dispatch"
            }
            known_premises.update(item.edge_id for item in analysis.bindings)
            known_premises.update(item.edge_id for item in analysis.derived_edges)
            if not set(record.premise_edge_ids) <= known_premises:
                outcome = ReplayOutcome.PREMISE_MISSING
            elif record.extractor_version != "1":
                outcome = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
            else:
                outcome = (
                    ReplayOutcome.PASS
                    if derived_edges.get(record.edge_id) == record.premise_edge_ids
                    else ReplayOutcome.EDGE_NOT_REPRODUCED
                )
        elif record.rule_id.startswith("imports."):
            outcome = (
                ReplayOutcome.PASS
                if record.edge_id in import_edges
                else ReplayOutcome.EDGE_NOT_REPRODUCED
            )
        elif current_parser_version is None:
            outcome = ReplayOutcome.ERROR
        elif record.extractor_version != current_parser_version:
            outcome = ReplayOutcome.EXTRACTOR_VERSION_MISMATCH
        elif record.file_path in parser_failures:
            outcome = parser_failures[record.file_path]
        else:
            outcome = (
                ReplayOutcome.PASS
                if record.edge_id in parser_edges.get(record.file_path, set())
                else ReplayOutcome.EDGE_NOT_REPRODUCED
            )
        results.append(
            _result(
                record.edge_id,
                scope,
                outcome,
                semantic_environment_id if scope == ReplayScope.SNAPSHOT_SEMANTIC else None,
            )
        )
    return results


def _scope(extractor_name: str, rule_id: str | None = None) -> ReplayScope:
    if rule_id and rule_id.startswith("imports."):
        return ReplayScope.FILE_OR_SPAN
    if extractor_name == "scip-python":
        return ReplayScope.SNAPSHOT_SEMANTIC
    if extractor_name == "static-dispatch":
        return ReplayScope.RULE_PREMISES
    return ReplayScope.FILE_OR_SPAN


def _result(
    edge_id: str,
    scope: ReplayScope,
    outcome: ReplayOutcome,
    environment_id: str | None = None,
    message: str | None = None,
) -> ReplayResult:
    return ReplayResult(
        edge_id=edge_id,
        scope=scope,
        outcome=outcome,
        review_state=ReviewState.NONE
        if outcome in {ReplayOutcome.PASS, ReplayOutcome.NOT_RUN}
        else ReviewState.REPLAY_FAILED,
        message=message
        or ("Evidence replay passed." if outcome == ReplayOutcome.PASS else outcome.value),
        verified_environment_id=environment_id,
    )

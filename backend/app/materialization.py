from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from .ids import stable_id
from .securefs import RepositoryPathEscape, read_repository_file


class RuntimeMaterializationError(ValueError):
    pass


class RuntimeFileEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    content_hash: str
    size_bytes: int
    executable: bool


class RuntimeMaterialization(BaseModel):
    model_config = ConfigDict(frozen=True)

    materialization_id: str
    snapshot_id: str
    include_untracked: bool
    files: list[RuntimeFileEntry]


def _tracked_paths(root: Path, include_untracked: bool) -> list[str]:
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if tracked.returncode:
        raise RuntimeMaterializationError("Could not enumerate Git-tracked runtime files")
    paths = {
        part.decode("utf-8", errors="surrogateescape")
        for part in tracked.stdout.split(b"\0")
        if part
    }
    if include_untracked:
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
        if untracked.returncode:
            raise RuntimeMaterializationError(
                "Could not enumerate permitted untracked runtime files"
            )
        paths.update(
            part.decode("utf-8", errors="surrogateescape")
            for part in untracked.stdout.split(b"\0")
            if part
        )
    safe_paths: list[str] = []
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise RuntimeMaterializationError(f"Unsafe Git runtime path rejected: {value!r}")
        if not path.parts or any(not part for part in path.parts):
            continue
        safe_paths.append(path.as_posix())
    return sorted(set(safe_paths))


def capture_runtime_materialization(
    repository_root: Path,
    snapshot_cache_dir: Path,
    snapshot_id: str,
    include_untracked: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> RuntimeMaterialization:
    """Capture the complete permitted checkout tree without adding graph inputs."""
    root = repository_root.resolve(strict=True)
    cache = snapshot_cache_dir.expanduser().resolve()
    snapshot_root = cache / snapshot_id
    runtime_root = snapshot_root / "runtime" / "materializations"
    runtime_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=".runtime-capture.", dir=runtime_root))
    files_root = temp_root / "files"
    files_root.mkdir()
    entries: list[RuntimeFileEntry] = []
    try:
        for relative in _tracked_paths(root, include_untracked):
            if check_cancelled is not None:
                check_cancelled()
            try:
                opened = read_repository_file(root, relative)
            except FileNotFoundError:
                # A tracked file deleted from the worktree is part of the dirty snapshot as absent.
                continue
            except RepositoryPathEscape:
                # Static ingestion reports PATH_REJECTED and omits external symlinks.
                continue
            if opened is None:
                continue
            content, metadata = opened
            destination = files_root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            try:
                with destination.open("xb") as output:
                    if check_cancelled is not None:
                        check_cancelled()
                    digest.update(content)
                    output.write(content)
            except OSError as exc:
                raise RuntimeMaterializationError(
                    f"Could not copy runtime file {relative}"
                ) from exc
            size_bytes = len(content)
            executable = bool(metadata.st_mode & 0o111)
            destination.chmod(0o555 if executable else 0o444)
            entries.append(
                RuntimeFileEntry(
                    path=relative,
                    content_hash=digest.hexdigest(),
                    size_bytes=size_bytes,
                    executable=executable,
                )
            )
        entries.sort(key=lambda item: item.path)
        materialization_id = stable_id(
            snapshot_id,
            include_untracked,
            [item.model_dump(mode="json") for item in entries],
            prefix="rtree_",
        )
        final_root = runtime_root / materialization_id
        if final_root.exists():
            existing = load_runtime_materialization(
                snapshot_cache_dir, snapshot_id, materialization_id
            )
            if existing.files != entries:
                raise RuntimeMaterializationError(
                    "Immutable runtime materialization identity mismatch"
                )
            shutil.rmtree(temp_root, ignore_errors=True)
            return existing
        payload = RuntimeMaterialization(
            materialization_id=materialization_id,
            snapshot_id=snapshot_id,
            include_untracked=include_untracked,
            files=entries,
        )
        (temp_root / "manifest.json").write_text(
            payload.model_dump_json(indent=2), encoding="utf-8"
        )
        (temp_root / "manifest.json").chmod(0o444)
        try:
            os.replace(temp_root, final_root)
        except FileExistsError:
            shutil.rmtree(temp_root, ignore_errors=True)
            existing = load_runtime_materialization(
                snapshot_cache_dir, snapshot_id, materialization_id
            )
            if existing.files != entries:
                raise RuntimeMaterializationError(
                    "Concurrent immutable runtime materialization mismatch"
                ) from None
            return existing
        return payload
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def load_runtime_materialization(
    snapshot_cache_dir: Path, snapshot_id: str, materialization_id: str
) -> RuntimeMaterialization:
    path = (
        snapshot_cache_dir.expanduser().resolve()
        / snapshot_id
        / "runtime"
        / "materializations"
        / materialization_id
        / "manifest.json"
    )
    try:
        payload = RuntimeMaterialization.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeMaterializationError(
            "Runtime snapshot materialization is unavailable"
        ) from exc
    if payload.snapshot_id != snapshot_id or payload.materialization_id != materialization_id:
        raise RuntimeMaterializationError("Runtime materialization identity mismatch")
    verify_runtime_files_root(path.parent / "files", snapshot_id, materialization_id)
    return payload


def verify_runtime_files_root(
    files_root: Path, snapshot_id: str, materialization_id: str | None = None
) -> RuntimeMaterialization:
    """Revalidate the exact materialized tree immediately before container use."""
    manifest_path = files_root.parent / "manifest.json"
    try:
        payload = RuntimeMaterialization.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        materialization_root = manifest_path.parent.resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
        resolved_files_root = files_root.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RuntimeMaterializationError(
            "Runtime snapshot materialization is unavailable"
        ) from exc
    if (
        payload.snapshot_id != snapshot_id
        or (materialization_id is not None and payload.materialization_id != materialization_id)
        or resolved_manifest.parent != materialization_root
        or resolved_files_root != materialization_root / "files"
        or not resolved_files_root.is_dir()
    ):
        raise RuntimeMaterializationError("Runtime materialization identity or path mismatch")
    expected_id = stable_id(
        payload.snapshot_id,
        payload.include_untracked,
        [item.model_dump(mode="json") for item in payload.files],
        prefix="rtree_",
    )
    if (
        payload.materialization_id != expected_id
        or payload.materialization_id != files_root.parent.name
    ):
        raise RuntimeMaterializationError("Runtime materialization content identity mismatch")
    expected_files: set[str] = set()
    expected_directories: set[str] = set()
    for entry in payload.files:
        relative = PurePosixPath(entry.path)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise RuntimeMaterializationError("Unsafe path in runtime materialization manifest")
        expected_files.add(relative.as_posix())
        expected_directories.update(
            PurePosixPath(*relative.parts[:depth]).as_posix()
            for depth in range(1, len(relative.parts))
        )
        file_path = resolved_files_root / Path(entry.path)
        try:
            resolved = file_path.resolve(strict=True)
            if resolved != resolved_files_root and resolved_files_root not in resolved.parents:
                raise RuntimeMaterializationError(
                    "Runtime materialization contains an escaping path"
                )
            if file_path.is_symlink() or not resolved.is_file():
                raise RuntimeMaterializationError("Runtime materialization contains an unsafe file")
            executable = bool(resolved.stat().st_mode & 0o111)
            if executable != entry.executable:
                raise RuntimeMaterializationError(
                    "Runtime materialization executable mode mismatch"
                )
            digest = hashlib.sha256()
            size_bytes = 0
            with resolved.open("rb") as content:
                while chunk := content.read(1024 * 1024):
                    digest.update(chunk)
                    size_bytes += len(chunk)
        except OSError as exc:
            raise RuntimeMaterializationError("Runtime materialization file is missing") from exc
        if digest.hexdigest() != entry.content_hash or size_bytes != entry.size_bytes:
            raise RuntimeMaterializationError("Runtime materialization content hash mismatch")
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for item in resolved_files_root.rglob("*"):
        relative_path = item.relative_to(resolved_files_root).as_posix()
        if item.is_symlink():
            raise RuntimeMaterializationError("Runtime materialization contains a symlink")
        if item.is_dir():
            actual_directories.add(relative_path)
        elif item.is_file():
            actual_files.add(relative_path)
        else:
            raise RuntimeMaterializationError("Runtime materialization contains a special file")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise RuntimeMaterializationError("Runtime materialization contains unlisted paths")
    return payload


def runtime_files_root(snapshot_cache_dir: Path, snapshot_id: str, materialization_id: str) -> Path:
    load_runtime_materialization(snapshot_cache_dir, snapshot_id, materialization_id)
    return (
        snapshot_cache_dir.expanduser().resolve()
        / snapshot_id
        / "runtime"
        / "materializations"
        / materialization_id
        / "files"
    )

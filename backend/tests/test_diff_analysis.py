from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app import diff_analysis, main
from backend.app.config import Settings
from backend.app.diff_analysis import (
    ChangedSymbol,
    CommitDiffService,
    DiffAnalysisRequest,
    DiffAnalysisResponse,
    DiffEvidence,
    ImpactPath,
    M21DiffError,
    _impact_paths,
    _materialize_git_tree,
    _parse_changed_files,
    _with_ranges,
    build_test_plan,
    map_changed_symbols,
)
from backend.app.diff_analysis import TestPlan as M21TestPlan
from backend.app.graph_tools import DeterministicGraphTools, ImpactToolRequest
from backend.app.history import RENAME_THRESHOLD
from backend.app.ingestion import analyze_repository, inspect_repository
from backend.app.models import (
    GitChangeStatus,
    SnapshotManifestEntry,
    SnapshotStatus,
    SourceEntity,
    SourceSpan,
)
from backend.app.runtime import identify_pytest_items

M21_EVALUATION = Path(__file__).parent / "fixtures" / "m21-diff-v1.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=M21 Test",
            "-c",
            "user.email=m21@example.test",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )
    return _git(root, "rev-parse", "HEAD")


def _history(tmp_path: Path) -> tuple[Path, Settings, str, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "mod.py").write_text(
        "class Base:\n    def method(self):\n        return 1\n\n"
        "class Child(Base):\n    def method(self):\n        return 0\n\n"
        "def change_me():\n    return 1\n\ndef removed():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "pkg" / "old.py").write_text("def moved():\n    return 7\n", encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(
        "from pkg.mod import change_me\n\ndef test_change_me():\n    assert change_me() == 2\n",
        encoding="utf-8",
    )
    base_sha = _commit(root, "base")
    (root / "pkg" / "mod.py").write_text(
        "class Base:\n    def method(self):\n        return 2\n\n"
        "class Child(Base):\n    def method(self):\n        return 0\n\n"
        "def change_me():\n    return 2\n\ndef added():\n    return 3\n",
        encoding="utf-8",
    )
    (root / "pkg" / "new.py").write_text(
        (root / "pkg" / "old.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "pkg" / "old.py").unlink()
    target_sha = _commit(root, "target")
    project_root = Path(__file__).resolve().parents[2]
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache" / "snapshots",
        runtime_container_image=None,
        scip_python_bin=str(project_root / "node_modules" / ".bin" / "scip-python"),
        scip_cli_bin=str(project_root / ".tools" / "scip" / "scip"),
    )
    repository_root, repository = inspect_repository(root, settings)
    return repository_root, settings, repository.repository_id, base_sha, target_sha


def _analyses(
    root: Path,
    settings: Settings,
    repository_id: str,
    base_sha: str,
    target_sha: str,
) -> tuple[object, object, CommitDiffService]:
    service = CommitDiffService(settings, jobs=object())  # type: ignore[arg-type]
    analyses = []
    for sha in (base_sha, target_sha):
        with service._commit_tree(root, repository_id, None, sha) as commit_root:
            analysis = analyze_repository(
                commit_root,
                settings,
                repository_id_override=repository_id,
                skip_worktree_status=True,
            )
            analyses.append(analysis.model_copy(update={"status": SnapshotStatus.READY}))
    return analyses[0], analyses[1], service


def test_commit_diff_maps_added_deleted_modified_and_renamed_symbols(tmp_path: Path) -> None:
    root, settings, repository_id, base_sha, target_sha = _history(tmp_path)
    base, target, _ = _analyses(root, settings, repository_id, base_sha, target_sha)
    paths = _parse_changed_files(root, repository_id, base_sha, target_sha)
    assert RENAME_THRESHOLD == 50
    renamed = next(item for item in paths if item.status == GitChangeStatus.RENAMED)
    assert (renamed.old_path, renamed.new_path, renamed.rename_similarity) == (
        "pkg/old.py",
        "pkg/new.py",
        100,
    )
    paths = [_with_ranges(root, base_sha, target_sha, item) for item in paths]
    symbols = map_changed_symbols(repository_id, base_sha, target_sha, base, target, paths)
    expected = json.loads(M21_EVALUATION.read_text(encoding="utf-8"))
    actual_changes = {
        (
            item.classification,
            item.base_qualified_name,
            item.target_qualified_name,
        )
        for item in symbols
    }
    expected_changes = {
        (
            item["classification"],
            item["base_qualified_name"],
            item["target_qualified_name"],
        )
        for item in expected["expected_symbol_changes"]
    }
    assert actual_changes == expected_changes
    assert len(symbols) == len(expected_changes)
    assert all(item.base_snapshot_id == base.snapshot_id for item in symbols)
    assert all(item.target_snapshot_id == target.snapshot_id for item in symbols)
    assert all(item.evidence for item in symbols)
    changed_base_method = next(
        item for item in symbols if item.target_qualified_name == "pkg.mod.Base.method"
    )
    target_entity = next(
        item for item in target.entities if item.entity_id == changed_base_method.target_entity_id
    )
    child_method = next(
        item for item in target.entities if item.qualified_name == "pkg.mod.Child.method"
    )
    impact = DeterministicGraphTools(target).find_impact(
        ImpactToolRequest(
            snapshot_id=target.snapshot_id,
            target_id=target_entity.entity_id,
            mode="STRICT_STATIC_BINDING",
            hop_limit=3,
            limit=20,
        )
    )
    child_impact = next(
        item for item in impact.results if item["node_id"] == child_method.entity_id
    )
    assert child_impact["steps"][0]["relation"] == "OVERRIDES"


def test_low_similarity_cross_file_move_is_reported_once(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    old_body = (
        "def moved():\n"
        + "".join(f"    value_{index} = {index}\n" for index in range(20))
        + "    return value_19\n"
    )
    new_body = (
        "def moved():\n"
        + "".join(f"    result_{index} = 'new-{index}'\n" for index in range(20))
        + "    return result_19\n"
    )
    (root / "pkg" / "old.py").write_text(old_body, encoding="utf-8")
    base_sha = _commit(root, "base")
    (root / "pkg" / "old.py").unlink()
    (root / "pkg" / "new.py").write_text(new_body, encoding="utf-8")
    target_sha = _commit(root, "move with rewritten body")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache" / "snapshots",
        runtime_container_image=None,
    )
    repository_root, repository = inspect_repository(root, settings)
    base, target, _ = _analyses(
        repository_root, settings, repository.repository_id, base_sha, target_sha
    )
    file_changes = _parse_changed_files(
        repository_root, repository.repository_id, base_sha, target_sha
    )
    assert {item.status for item in file_changes} == {
        GitChangeStatus.ADDED,
        GitChangeStatus.DELETED,
    }
    changes = [_with_ranges(repository_root, base_sha, target_sha, item) for item in file_changes]

    symbols = map_changed_symbols(
        repository.repository_id, base_sha, target_sha, base, target, changes
    )

    assert len(symbols) == 1
    assert symbols[0].classification == "RENAMED_OR_MOVED"
    assert (symbols[0].base_file_path, symbols[0].target_file_path) == (
        "pkg/old.py",
        "pkg/new.py",
    )


def _symbol_snapshot(snapshot_id: str, files: dict[str, list[tuple[str, str]]]):
    from backend.tests.test_graph_tools import _analysis as graph_analysis

    seed = graph_analysis()
    entities = []
    manifest = []
    source: dict[str, bytes] = {}
    for path, symbols in sorted(files.items()):
        content = b""
        placements = []
        for name, parent_name in symbols:
            body = f"def {name}():\n    return 1\n".encode()
            start = len(content)
            content += body
            placements.append((name, parent_name, start, len(content)))
        source[path] = content
        digest = hashlib.sha256(content).hexdigest()
        manifest.append(
            SnapshotManifestEntry(
                path=path,
                content_hash=digest,
                blob_hash=digest,
                size_bytes=len(content),
                analyzed=True,
            )
        )
        module_id = f"module:{snapshot_id}:{path}"
        entities.append(
            SourceEntity(
                entity_id=module_id,
                kind="Module",
                name=Path(path).stem,
                qualified_name=Path(path).stem,
                file_path=path,
                span=SourceSpan(
                    start_byte=0,
                    end_byte=len(content),
                    start_line=1,
                    start_column=0,
                    end_line=max(1, content.count(b"\n")),
                    end_column=0,
                ),
            )
        )
        for index, (name, parent_name, start, end) in enumerate(placements):
            start_line = content[:start].count(b"\n") + 1
            end_line = content[:end].count(b"\n")
            parent_id = module_id
            if parent_name != "module":
                parent_id = f"class:{snapshot_id}:{path}:{parent_name}"
                if parent_id not in {entity.entity_id for entity in entities}:
                    entities.append(
                        SourceEntity(
                            entity_id=parent_id,
                            kind="Class",
                            name=parent_name,
                            qualified_name=f"{Path(path).stem}.{parent_name}",
                            file_path=path,
                            span=SourceSpan(
                                start_byte=start,
                                end_byte=end,
                                start_line=start_line,
                                start_column=0,
                                end_line=end_line,
                                end_column=0,
                            ),
                            parent_id=module_id,
                        )
                    )
            entities.append(
                SourceEntity(
                    entity_id=f"symbol:{snapshot_id}:{path}:{index}:{parent_name}",
                    kind="Function" if parent_name == "module" else "Method",
                    name=name,
                    qualified_name=f"{Path(path).stem}.{parent_name}.{name}",
                    file_path=path,
                    span=SourceSpan(
                        start_byte=start,
                        end_byte=end,
                        start_line=start_line,
                        start_column=0,
                        end_line=end_line,
                        end_column=0,
                    ),
                    parent_id=parent_id,
                )
            )
    analysis = seed.model_copy(
        update={
            "snapshot_id": snapshot_id,
            "manifest": manifest,
            "entities": entities,
            "sites": [],
            "provenance": [],
            "bindings": [],
            "imports": [],
            "derived_edges": [],
            "dispatch": [],
            "runtime_materialization_id": f"materialization:{snapshot_id}",
        }
    )
    return analysis, source


def _file_change(status: GitChangeStatus, path: str) -> diff_analysis.ChangedFile:
    ranges = (diff_analysis.DiffRange(start_line=1, line_count=20),)
    return diff_analysis.ChangedFile(
        change_id=f"{status.value}:{path}",
        status=status,
        old_path=path if status == GitChangeStatus.DELETED else None,
        new_path=path if status == GitChangeStatus.ADDED else None,
        base_ranges=ranges if status == GitChangeStatus.DELETED else (),
        target_ranges=ranges if status == GitChangeStatus.ADDED else (),
    )


def test_added_symbol_is_not_paired_with_unchanged_counterpart_elsewhere() -> None:
    base, base_source = _symbol_snapshot("base", {"pkg/existing.py": [("helper", "module")]})
    target, target_source = _symbol_snapshot(
        "target",
        {
            "pkg/existing.py": [("helper", "module")],
            "pkg/new.py": [("helper", "module")],
        },
    )
    added = _file_change(GitChangeStatus.ADDED, "pkg/new.py")

    result = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        [added],
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )

    assert len(result) == 1
    assert result[0].classification == "ADDED"
    assert result[0].base_entity_id is None
    assert result[0].target_file_path == "pkg/new.py"


def test_unique_deleted_to_added_symbol_move_is_mapped_once_and_order_independently() -> None:
    base, base_source = _symbol_snapshot("base", {"pkg/old.py": [("helper", "module")]})
    target, target_source = _symbol_snapshot("target", {"pkg/new.py": [("helper", "module")]})
    changes = [
        _file_change(GitChangeStatus.DELETED, "pkg/old.py"),
        _file_change(GitChangeStatus.ADDED, "pkg/new.py"),
    ]

    forward = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        changes,
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )
    reverse = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        list(reversed(changes)),
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )

    assert [item.model_dump() for item in forward] == [item.model_dump() for item in reverse]
    assert len(forward) == 1
    assert forward[0].classification == "RENAMED_OR_MOVED"
    assert (forward[0].base_file_path, forward[0].target_file_path) == ("pkg/old.py", "pkg/new.py")


def test_ambiguous_same_signature_candidates_are_never_paired() -> None:
    base, base_source = _symbol_snapshot(
        "base",
        {"pkg/old_a.py": [("helper", "module")], "pkg/old_b.py": [("helper", "module")]},
    )
    target, target_source = _symbol_snapshot(
        "target",
        {"pkg/new_a.py": [("helper", "module")], "pkg/new_b.py": [("helper", "module")]},
    )
    changes = [
        _file_change(GitChangeStatus.DELETED, "pkg/old_a.py"),
        _file_change(GitChangeStatus.DELETED, "pkg/old_b.py"),
        _file_change(GitChangeStatus.ADDED, "pkg/new_a.py"),
        _file_change(GitChangeStatus.ADDED, "pkg/new_b.py"),
    ]

    result = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        changes,
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )

    assert not any(item.classification == "RENAMED_OR_MOVED" for item in result)
    assert sum(item.classification == "DELETED" for item in result) == 2
    assert sum(item.classification == "ADDED" for item in result) == 2
    assert (
        len({(item.change_id, item.base_entity_id, item.target_entity_id) for item in result}) == 4
    )


def test_same_name_methods_in_different_classes_are_not_cross_paired() -> None:
    base, base_source = _symbol_snapshot("base", {"pkg/old.py": [("run", "Alpha")]})
    target, target_source = _symbol_snapshot("target", {"pkg/new.py": [("run", "Beta")]})
    changes = [
        _file_change(GitChangeStatus.DELETED, "pkg/old.py"),
        _file_change(GitChangeStatus.ADDED, "pkg/new.py"),
    ]

    result = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        changes,
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )

    assert [item.classification for item in result] == ["ADDED", "DELETED"]


def test_same_name_module_symbols_with_distinct_surviving_modules_are_not_cross_paired() -> None:
    base, base_source = _symbol_snapshot(
        "base",
        {"pkg/old.py": [("helper", "module")], "pkg/other.py": [("helper", "module")]},
    )
    target, target_source = _symbol_snapshot(
        "target",
        {"pkg/new.py": [("helper", "module")], "pkg/other.py": [("helper", "module")]},
    )
    changes = [
        _file_change(GitChangeStatus.DELETED, "pkg/old.py"),
        _file_change(GitChangeStatus.ADDED, "pkg/new.py"),
    ]

    result = map_changed_symbols(
        "repo",
        "a" * 40,
        "b" * 40,
        base,
        target,
        changes,
        base_source_loader=base_source.get,
        target_source_loader=target_source.get,
    )

    assert not any(item.classification == "RENAMED_OR_MOVED" for item in result)
    assert sorted(item.classification for item in result) == ["ADDED", "DELETED"]


def test_git_tree_materialization_is_exact_and_does_not_run_filters(tmp_path: Path) -> None:
    root, settings, repository_id, _base_sha, target_sha = _history(tmp_path)
    marker = root / "filter-was-run"
    _git(root, "config", "filter.m21.smudge", f"touch {marker}")
    (root / ".gitattributes").write_text("pkg/mod.py filter=m21\n", encoding="utf-8")
    target_sha = _commit(root, "add filter attribute")
    service = CommitDiffService(settings, jobs=object())  # type: ignore[arg-type]
    with service._commit_tree(root, repository_id, None, target_sha) as snapshot_root:
        expected = subprocess.run(
            ["git", "-C", str(root), "show", f"{target_sha}:pkg/mod.py"],
            check=True,
            capture_output=True,
        ).stdout
        assert (snapshot_root / "pkg" / "mod.py").read_bytes() == expected
        assert (snapshot_root / ".git").is_dir()
    assert not marker.exists()
    assert not list((root / ".codegraph-cache" / "m21-commits").iterdir())


def test_invalid_git_path_fails_closed_and_identical_commit_diff_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=M21 Test",
            "-c",
            "user.email=m21@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )
    base_sha = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(diff_analysis, "_git", lambda *_args, **_kwargs: b"invalid-\xff\0")
    with pytest.raises(M21DiffError, match="UTF-8"):
        _parse_changed_files(root, "repo_" + "1" * 64, base_sha, base_sha)
    monkeypatch.undo()
    assert _parse_changed_files(root, "repo_" + "1" * 64, base_sha, base_sha) == []


def test_git_command_output_limit_is_enforced_while_streaming(monkeypatch) -> None:
    monkeypatch.setattr(diff_analysis, "MAX_DIFF_OUTPUT_BYTES", 8)
    with pytest.raises(M21DiffError, match="bounded analysis limit"):
        diff_analysis._git(Path("/"), "--version")


def _single_commit_repo(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str]:
    root = tmp_path / "bounded-repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root, _commit(root, "commit fixture")


def test_commit_materialization_rejects_per_file_and_cumulative_limits(tmp_path: Path) -> None:
    root, sha = _single_commit_repo(tmp_path, {"large.bin": b"123456789"})
    destination = tmp_path / "per-file"
    destination.mkdir()
    with pytest.raises(M21DiffError, match="per-file limit"):
        _materialize_git_tree(root, destination, sha, max_blob_bytes=8, max_total_bytes=100)
    assert not destination.exists()

    root, sha = _single_commit_repo(tmp_path / "cumulative", {"a.bin": b"1234", "b.bin": b"5678"})
    destination = tmp_path / "cumulative-output"
    destination.mkdir()
    with pytest.raises(M21DiffError, match="total-byte limit"):
        _materialize_git_tree(root, destination, sha, max_blob_bytes=8, max_total_bytes=6)
    assert not destination.exists()


def _fake_cat_file(monkeypatch: pytest.MonkeyPatch, script: str) -> list[int]:
    real_popen = subprocess.Popen
    child_pids: list[int] = []

    def popen(command, *args, **kwargs):
        if command[-2:] == ["cat-file", "--batch"]:
            process = real_popen([sys.executable, "-c", script], *args, **kwargs)
            child_pids.append(process.pid)
            return process
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(diff_analysis.subprocess, "Popen", popen)
    return child_pids


def test_commit_materialization_rejects_streamed_bytes_beyond_git_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = _single_commit_repo(tmp_path, {"payload.bin": b"abc"})
    child_pids = _fake_cat_file(
        monkeypatch,
        "import sys\nfor line in sys.stdin.buffer:\n oid=line.strip().decode(); "
        "sys.stdout.buffer.write(f'{oid} blob 2\\nabc\\n'.encode()); sys.stdout.flush()\n",
    )
    destination = tmp_path / "stream-mismatch"
    destination.mkdir()

    with pytest.raises(M21DiffError, match="invalid blob record"):
        _materialize_git_tree(root, destination, sha)

    assert not destination.exists()
    assert child_pids
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


def test_commit_materialization_timeout_kills_process_group_and_cleans_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = _single_commit_repo(tmp_path, {"payload.bin": b"abc"})
    child_pids = _fake_cat_file(
        monkeypatch,
        "import sys,time\nsys.stdin.buffer.readline(); time.sleep(10)\n",
    )
    destination = tmp_path / "timeout"
    destination.mkdir()
    started = time.monotonic()

    with pytest.raises(M21DiffError, match="wall-clock deadline"):
        _materialize_git_tree(root, destination, sha, timeout_seconds=1)

    assert time.monotonic() - started < 4
    assert not destination.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


def test_commit_materialization_cleans_partial_tree_after_subprocess_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = _single_commit_repo(tmp_path, {"a.bin": b"first", "b.bin": b"second"})
    _fake_cat_file(
        monkeypatch,
        "import sys\ncount=0\nfor line in sys.stdin.buffer:\n "
        "oid=line.strip().decode()\n "
        "sys.stdout.buffer.write((f'{oid} blob 5\\nfirst\\n' "
        "if count == 0 else 'invalid\\n').encode())\n "
        "sys.stdout.flush(); count += 1\n",
    )
    destination = tmp_path / "partial"
    destination.mkdir()

    with pytest.raises(M21DiffError, match="unexpected blob response"):
        _materialize_git_tree(root, destination, sha)

    assert not destination.exists()


def test_materialization_limit_failure_never_starts_snapshot_ingestion(tmp_path: Path) -> None:
    root, sha = _single_commit_repo(tmp_path, {"too-large.py": b"x" * 128})
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache" / "snapshots",
        runtime_container_image=None,
        m21_max_commit_blob_bytes=16,
    )
    service = CommitDiffService(settings, jobs=object())  # type: ignore[arg-type]
    snapshot_calls: list[str] = []
    service._snapshot = lambda *_args: snapshot_calls.append("called")  # type: ignore[method-assign]

    with pytest.raises(M21DiffError, match="per-file limit"):
        service.analyze(
            DiffAnalysisRequest(repository_path=str(root), base_sha=sha, target_sha=sha)
        )

    assert snapshot_calls == []
    assert not list((root / ".codegraph-cache" / "m21-commits").glob("*"))


def test_unsafe_committed_symlink_is_rejected_and_temp_tree_cleaned(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "escape.py").symlink_to("../../outside.py")
    sha = _commit(root, "unsafe symlink")
    settings = Settings(allowed_repository_roots=[tmp_path], snapshot_cache_dir=tmp_path / "cache")
    _, repository = inspect_repository(root, settings)
    service = CommitDiffService(settings, jobs=object())  # type: ignore[arg-type]
    with pytest.raises(M21DiffError, match="symlink"):
        with service._commit_tree(root, repository.repository_id, None, sha):
            pass
    assert not list((root / ".codegraph-cache" / "m21-commits").iterdir())


def test_tier_c_is_deterministic_and_discloses_missing_runtime_baseline(tmp_path: Path) -> None:
    root, settings, repository_id, base_sha, target_sha = _history(tmp_path)
    base, target, _ = _analyses(root, settings, repository_id, base_sha, target_sha)
    paths = [
        _with_ranges(root, base_sha, target_sha, item)
        for item in _parse_changed_files(root, repository_id, base_sha, target_sha)
    ]
    symbols = map_changed_symbols(repository_id, base_sha, target_sha, base, target, paths)
    target_tools = DeterministicGraphTools(target)
    definitions, invocations = identify_pytest_items(
        target.snapshot_id,
        ["tests/test_mod.py::test_change_me", "tests/test_mod.py::test_change_me[case-2]"],
    )
    first = build_test_plan(
        target,
        target_tools,
        symbols,
        (),
        test_definitions=definitions,
        test_invocations=invocations,
        collection_run_id="collection-run-target",
        collection_environment_id="environment-target",
        collection_materialization_id=target.runtime_materialization_id,
        source_loader=lambda path: (root / path).read_bytes() if (root / path).is_file() else None,
    )
    second = build_test_plan(
        target,
        target_tools,
        symbols,
        (),
        test_definitions=definitions,
        test_invocations=invocations,
        collection_run_id="collection-run-target",
        collection_environment_id="environment-target",
        collection_materialization_id=target.runtime_materialization_id,
        source_loader=lambda path: (root / path).read_bytes() if (root / path).is_file() else None,
    )
    assert not first.runtime_baseline_available
    assert first.tiers == {
        "A": "UNAVAILABLE_NO_RUNTIME_BASELINE",
        "B": "UNAVAILABLE_NO_RUNTIME_BASELINE",
        "C": "AVAILABLE",
    }
    assert [item.pytest_node_id for item in first.recommendations] == [
        "tests/test_mod.py::test_change_me",
        "tests/test_mod.py::test_change_me[case-2]",
    ]
    assert [item.model_dump(mode="json") for item in first.recommendations] == [
        item.model_dump(mode="json") for item in second.recommendations
    ]
    assert all(item.tier == "C" and item.evidence for item in first.recommendations)
    assert all(
        any(evidence.evidence_type == "SOURCE_RANGE" for evidence in item.evidence)
        for item in first.recommendations
    )
    assert all(
        any(
            evidence.evidence_type == "TEST_COLLECTION"
            and evidence.snapshot_id == target.snapshot_id
            and evidence.record_id == "collection-run-target"
            and evidence.runtime_materialization_id == target.runtime_materialization_id
            and evidence.runtime_environment_id == "environment-target"
            and evidence.run_id == "collection-run-target"
            for evidence in item.evidence
        )
        for item in first.recommendations
    )
    assert any("No runtime baseline" in item for item in first.diagnostics)


def test_tier_c_rejects_cross_snapshot_test_ids_and_reports_collection_unavailable(
    tmp_path: Path,
) -> None:
    root, settings, repository_id, base_sha, target_sha = _history(tmp_path)
    base, target, _ = _analyses(root, settings, repository_id, base_sha, target_sha)
    paths = [
        _with_ranges(root, base_sha, target_sha, item)
        for item in _parse_changed_files(root, repository_id, base_sha, target_sha)
    ]
    symbols = map_changed_symbols(repository_id, base_sha, target_sha, base, target, paths)
    plan = build_test_plan(
        target,
        DeterministicGraphTools(target),
        symbols,
        (),
        collection_status="UNAVAILABLE",
        collection_error="Docker is unavailable",
        source_loader=lambda _path: b"",
    )
    assert plan.recommendations == ()
    assert plan.tiers["C"] == "UNAVAILABLE_COLLECTION"
    assert any("Docker is unavailable" in item for item in plan.diagnostics)


def test_diff_request_validates_commit_ids_and_rejects_arbitrary_fields() -> None:
    with pytest.raises(ValueError):
        DiffAnalysisRequest(repository_path="/repo", base_sha="--exec", target_sha="a" * 40)
    with pytest.raises(ValueError):
        DiffAnalysisRequest(
            repository_path="/repo", base_sha="a" * 40, target_sha="b" * 40, command="pytest"
        )


def test_runtime_evidence_selects_tier_a_and_tier_b_without_unioning_snapshots() -> None:
    from backend.tests.test_graph_tools import SNAPSHOT as graph_snapshot
    from backend.tests.test_graph_tools import _analysis as graph_analysis

    target = graph_analysis().model_copy(
        update={
            "status": SnapshotStatus.READY,
            "runtime_materialization_id": "materialization-target",
        }
    )
    changed = ChangedSymbol(
        change_id="diff-1",
        classification="MODIFIED",
        base_snapshot_id="snap-base",
        target_snapshot_id=graph_snapshot,
        base_entity_id="old-callee",
        target_entity_id="callee",
        base_qualified_name="b.callee",
        target_qualified_name="b.callee",
        base_file_path="b.py",
        target_file_path="b.py",
        deterministic_reason="Changed source range intersects this method.",
        evidence=(
            DiffEvidence(
                evidence_type="SOURCE_RANGE",
                snapshot_id=graph_snapshot,
                record_id="range-1",
                file_path="b.py",
                entity_id="callee",
                start_line=1,
                end_line=2,
            ),
        ),
    )
    tool = DeterministicGraphTools(
        target,
        runtime_records=(
            {
                "snapshot_id": graph_snapshot,
                "runtime_materialization_id": "materialization-target",
                "runtime_environment_id": "environment-target",
                "entity_id": "callee",
                "test_definition_id": "test-direct",
                "test_invocation_id": "inv-direct",
                "node_id": "tests/test_change.py::test_direct",
                "entry_id": "entry-direct",
                "run_id": "run-direct",
                "observation_kind": "FUNCTION_ENTRY",
            },
            {
                "snapshot_id": graph_snapshot,
                "runtime_materialization_id": "materialization-target",
                "runtime_environment_id": "environment-target",
                "entity_id": "caller",
                "test_definition_id": "test-dependent",
                "test_invocation_id": "inv-dependent",
                "node_id": "tests/test_change.py::test_dependent",
                "entry_id": "entry-dependent",
                "run_id": "run-dependent",
                "observation_kind": "FUNCTION_ENTRY",
            },
            {
                "snapshot_id": "snap-other",
                "entity_id": "callee",
                "test_definition_id": "wrong-snapshot-test",
                "test_invocation_id": "wrong-snapshot-invocation",
                "node_id": "tests/test_wrong.py::test_wrong",
                "entry_id": "entry-other",
                "observation_kind": "FUNCTION_ENTRY",
            },
        ),
    )
    raw_impact = tool.find_impact(
        ImpactToolRequest(
            snapshot_id=graph_snapshot,
            target_id="callee",
            mode="CONSERVATIVE_DISPATCH",
            hop_limit=3,
            limit=20,
        )
    ).results
    caller = next(item for item in raw_impact if item["node_id"] == "caller")
    impact = ImpactPath(
        change_id="diff-1",
        snapshot_id=graph_snapshot,
        changed_entity_id="callee",
        dependent_entity_id="caller",
        distance=caller["distance"],
        mode=caller["mode"],
        path_semantics="STRICT_EXACT",
        evidence_complete=True,
        uncertainty_count=caller["uncertainty_count"],
        path=tuple(caller["path"]),
        steps=tuple(caller["steps"]),
        evidence=(
            DiffEvidence(
                evidence_type="VERIFIED_GRAPH_PATH",
                snapshot_id=graph_snapshot,
                record_id="edge-caller",
                evidence_id="evidence-caller",
                entity_id="caller",
            ),
        ),
    )
    definitions, invocations = identify_pytest_items(
        graph_snapshot,
        ["tests/test_change.py::test_direct", "tests/test_change.py::test_dependent"],
    )
    plan = build_test_plan(
        target,
        tool,
        [changed],
        [impact],
        test_definitions=definitions,
        test_invocations=invocations,
        collection_status="COMPLETED",
        collection_environment_id="environment-target",
        collection_materialization_id="materialization-target",
    )
    tiers = {item.pytest_node_id: item.tier for item in plan.recommendations}
    assert tiers == {
        "tests/test_change.py::test_direct": "A",
        "tests/test_change.py::test_dependent": "B",
    }
    assert all(item.evidence for item in plan.recommendations)
    assert all(item.test_definition_id != "wrong-snapshot-test" for item in plan.recommendations)
    recommendation = next(item for item in plan.recommendations if item.tier == "B")
    assert recommendation.impact_path is not None
    assert recommendation.impact_path.path == tuple(caller["path"])
    assert recommendation.impact_path.evidence[0].evidence_id == "evidence-caller"
    assert recommendation.evidence[0].runtime_environment_id == "environment-target"
    assert recommendation.evidence[0].evidence_id
    assert recommendation.evidence[0].record_id


@pytest.mark.parametrize(
    ("runtime_materialization_id", "runtime_environment_id"),
    [
        ("materialization-old", "environment-target"),
        ("materialization-target", "environment-old"),
    ],
)
def test_same_snapshot_observations_from_incompatible_runtime_are_not_tier_a_or_b(
    runtime_materialization_id: str, runtime_environment_id: str
) -> None:
    from backend.tests.test_graph_tools import SNAPSHOT as graph_snapshot
    from backend.tests.test_graph_tools import _analysis as graph_analysis

    target = graph_analysis().model_copy(
        update={
            "status": SnapshotStatus.READY,
            "runtime_materialization_id": "materialization-target",
        }
    )
    changed = ChangedSymbol(
        change_id="diff-runtime",
        classification="MODIFIED",
        base_snapshot_id="snap-base",
        target_snapshot_id=graph_snapshot,
        base_entity_id="old-callee",
        target_entity_id="callee",
        base_qualified_name="b.callee",
        target_qualified_name="b.callee",
        base_file_path="b.py",
        target_file_path="b.py",
        deterministic_reason="Changed source range intersects this method.",
        evidence=(),
    )
    records = (
        {
            "snapshot_id": graph_snapshot,
            "runtime_materialization_id": runtime_materialization_id,
            "runtime_environment_id": runtime_environment_id,
            "entity_id": "callee",
            "test_definition_id": "test-reused",
            "test_invocation_id": "invocation-reused",
            "node_id": "tests/test_change.py::test_reused",
            "entry_id": f"entry-{runtime_environment_id}",
            "run_id": f"run-{runtime_environment_id}",
            "observation_kind": "FUNCTION_ENTRY",
        },
    )
    tool = DeterministicGraphTools(target, runtime_records=records)
    definitions, invocations = identify_pytest_items(
        graph_snapshot, ["tests/test_change.py::test_reused"]
    )

    plan = build_test_plan(
        target,
        tool,
        [changed],
        (),
        test_definitions=definitions,
        test_invocations=invocations,
        collection_status="COMPLETED",
        collection_materialization_id="materialization-target",
        collection_environment_id="environment-target",
    )

    assert plan.runtime_baseline_available is False
    assert not any(item.tier in {"A", "B"} for item in plan.recommendations)
    assert any("exact runtime materialization" in item for item in plan.diagnostics)


@pytest.mark.parametrize(
    ("target_id", "expected_path", "expected_semantics", "expected_distance"),
    [
        ("callee", ("caller", "callee"), "STRICT_EXACT", 1),
        ("derived-run", ("caller", "derived-run"), "CONSERVATIVE", 1),
        ("base-run", ("caller", "derived-run", "base-run"), "CONSERVATIVE", 2),
    ],
)
def test_impact_paths_preserve_graph_step_evidence_and_semantics(
    target_id: str,
    expected_path: tuple[str, ...],
    expected_semantics: str,
    expected_distance: int,
) -> None:
    from backend.tests.test_graph_tools import SNAPSHOT as graph_snapshot
    from backend.tests.test_graph_tools import _analysis as graph_analysis

    analysis = graph_analysis()
    symbol = ChangedSymbol(
        change_id="diff-path",
        classification="MODIFIED",
        base_snapshot_id="snap-base",
        target_snapshot_id=graph_snapshot,
        target_entity_id=target_id,
        deterministic_reason="Changed symbol test fixture.",
    )
    tools = DeterministicGraphTools(analysis)
    paths, truncated = _impact_paths(
        [symbol], analysis, analysis, tools, tools, hop_limit=3, result_limit=20
    )
    assert not truncated
    impact = next(item for item in paths if item.dependent_entity_id == "caller")
    assert impact.path == expected_path
    assert impact.path_semantics == expected_semantics
    assert impact.distance == expected_distance
    assert impact.evidence_complete
    assert len(impact.steps) == expected_distance
    assert all(step["edge_ids"] for step in impact.steps)
    assert impact.evidence
    assert all(item.evidence_id for item in impact.evidence)
    assert ImpactPath.model_validate_json(impact.model_dump_json()) == impact
    if expected_semantics == "CONSERVATIVE":
        assert impact.uncertainty_count > 0
        assert any(item["site_id"] == "call-unknown" for item in impact.unresolved_barriers)


def test_tier_b_is_not_promoted_when_impact_path_evidence_is_incomplete() -> None:
    from backend.tests.test_graph_tools import SNAPSHOT as graph_snapshot
    from backend.tests.test_graph_tools import _analysis as graph_analysis

    target = graph_analysis().model_copy(
        update={
            "status": SnapshotStatus.READY,
            "runtime_materialization_id": "materialization-target",
        }
    )
    changed = ChangedSymbol(
        change_id="diff-incomplete",
        classification="MODIFIED",
        base_snapshot_id="snap-base",
        target_snapshot_id=graph_snapshot,
        target_entity_id="callee",
        deterministic_reason="Changed source range intersects this method.",
    )
    runtime = {
        "snapshot_id": graph_snapshot,
        "runtime_materialization_id": "materialization-target",
        "runtime_environment_id": "environment-target",
        "entity_id": "caller",
        "test_definition_id": "test-caller",
        "test_invocation_id": "invocation-caller",
        "node_id": "tests/test_change.py::test_caller",
        "entry_id": "entry-caller",
        "run_id": "run-caller",
        "observation_kind": "FUNCTION_ENTRY",
    }
    impact = ImpactPath(
        change_id=changed.change_id,
        snapshot_id=graph_snapshot,
        changed_entity_id="callee",
        dependent_entity_id="caller",
        distance=1,
        mode="STRICT_STATIC_BINDING",
        path_semantics="INCOMPLETE",
        evidence_complete=False,
        uncertainty_count=0,
        path=("caller", "callee"),
        steps=(),
        evidence=(),
    )
    tool = DeterministicGraphTools(target, runtime_records=(runtime,))
    definitions, invocations = identify_pytest_items(
        graph_snapshot, ["tests/test_change.py::test_caller"]
    )
    plan = build_test_plan(
        target,
        tool,
        [changed],
        [impact],
        test_definitions=definitions,
        test_invocations=invocations,
        collection_status="COMPLETED",
        collection_materialization_id="materialization-target",
        collection_environment_id="environment-target",
    )
    assert not any(item.tier == "B" for item in plan.recommendations)


def test_diff_api_delegates_to_service_and_serializes_snapshot_scoped_response(monkeypatch) -> None:
    response = DiffAnalysisResponse(
        repository_id="repo_" + "1" * 64,
        base_commit_sha="a" * 40,
        target_commit_sha="b" * 40,
        base_snapshot_id="snap_" + "2" * 64,
        target_snapshot_id="snap_" + "3" * 64,
        changed_files=(),
        changed_symbols=(),
        impact_paths=(),
        test_plan=M21TestPlan(
            snapshot_id="snap_" + "3" * 64,
            runtime_baseline_available=False,
            collection_status="UNAVAILABLE",
            tiers={
                "A": "UNAVAILABLE_NO_RUNTIME_BASELINE",
                "B": "UNAVAILABLE_NO_RUNTIME_BASELINE",
                "C": "UNAVAILABLE_COLLECTION",
            },
            diagnostics=("Docker is unavailable",),
        ),
        candidate_count_before_limit=0,
    )

    class FakeService:
        def analyze(self, request: DiffAnalysisRequest) -> DiffAnalysisResponse:
            assert request.repository_path == "/approved/repository"
            assert request.base_sha == "a" * 40
            return response

    monkeypatch.setattr(main, "commit_diff_service", FakeService())
    actual = main.diff_analyze(
        DiffAnalysisRequest(
            repository_path="/approved/repository",
            base_sha="a" * 40,
            target_sha="b" * 40,
        )
    )
    assert actual == response
    assert actual.test_plan.snapshot_id == actual.target_snapshot_id

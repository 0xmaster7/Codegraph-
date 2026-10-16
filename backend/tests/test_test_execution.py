from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.ingestion import (
    _collect_manifest,
    _discover,
    identify_snapshot,
    inspect_repository,
)
from backend.app.main import RuntimeActionRequest
from backend.app.materialization import (
    RuntimeMaterializationError,
    capture_runtime_materialization,
    runtime_files_root,
    verify_runtime_files_root,
)
from backend.app.models import (
    ActionApprovalStatus,
    RuntimeRunStatus,
)
from backend.app.models import TestExecutionResult as RuntimeResult
from backend.app.test_execution import (
    ApprovalError,
    ApprovalRegistry,
    DockerTestExecutor,
    DockerUnavailable,
    RuntimeActionService,
    RuntimeExecutionError,
    command_identity_hash,
    deterministic_pytest_command,
    validate_node_id,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repository(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "runtime@example.invalid")
    _git(root, "config", "user.name", "Runtime Test")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "runtime fixture")
    return root


def _executor(image: str, *, timeout: int = 30) -> DockerTestExecutor:
    return DockerTestExecutor(
        Settings(
            runtime_container_image=image,
            runtime_timeout_seconds=timeout,
            runtime_memory_mb=256,
            runtime_cpu_count=0.5,
            runtime_pids_limit=64,
        )
    )


def _snapshot(tmp_path: Path, image: str, files: dict[str, str]):
    repo = _repository(tmp_path, files)
    cache = tmp_path / "cache"
    materialization = capture_runtime_materialization(repo, cache, "snap_" + "a" * 64)
    return (
        repo,
        cache,
        materialization,
        runtime_files_root(cache, materialization.snapshot_id, materialization.materialization_id),
    )


def _assert_no_runtime_containers() -> None:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=codegraph-test-"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stdout.strip()


def test_approval_requires_approval_exact_set_and_is_single_use() -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection(
        "snap_fixture",
        "runtime_fixture",
        ["tests/test_one.py::test_a", "tests/test_one.py::test_b"],
    )
    approval = registry.prepare("snap_fixture", "runtime_fixture", ids, "sha256:" + "1" * 64)
    assert approval.status == ActionApprovalStatus.PENDING
    assert approval.pytest_command == deterministic_pytest_command(
        ["tests/test_one.py::test_a", "tests/test_one.py::test_b"]
    )
    with pytest.raises(ApprovalError, match="user approval"):
        registry.consume(
            approval.approval_id,
            approval.snapshot_id,
            approval.runtime_environment_id,
            ids,
            "sha256:" + "1" * 64,
        )
    registry.approve(approval.approval_id)
    with pytest.raises(ApprovalError, match="test set"):
        registry.consume(
            approval.approval_id,
            approval.snapshot_id,
            approval.runtime_environment_id,
            ids[:1],
            "sha256:" + "1" * 64,
        )
    consumed, node_ids = registry.consume(
        approval.approval_id,
        approval.snapshot_id,
        approval.runtime_environment_id,
        ids,
        "sha256:" + "1" * 64,
    )
    assert consumed.status == ActionApprovalStatus.CONSUMED
    assert node_ids == ("tests/test_one.py::test_a", "tests/test_one.py::test_b")
    with pytest.raises(ApprovalError, match="user approval"):
        registry.consume(
            approval.approval_id,
            approval.snapshot_id,
            approval.runtime_environment_id,
            ids,
            "sha256:" + "1" * 64,
        )
    assert command_identity_hash(
        "s", "e", ["i"], ["tests/a.py::test_a"], "image"
    ) != command_identity_hash("s", "e", ["i"], ["tests/a.py::test_a"], "other-image")


def test_expired_approval_is_rejected() -> None:
    registry = ApprovalRegistry(ttl_seconds=0)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    approval = registry.prepare("s", "e", ids, "sha256:" + "1" * 64)
    with pytest.raises(ApprovalError, match="expired"):
        registry.approve(approval.approval_id)


def test_approval_expiring_before_consumption_cannot_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    approval = registry.prepare("s", "e", ids, "sha256:" + "1" * 64)
    registry.approve(approval.approval_id)
    monkeypatch.setattr("backend.app.test_execution.time.time", lambda: approval.expires_at)
    with pytest.raises(ApprovalError, match="expired"):
        registry.consume(approval.approval_id, "s", "e", ids, "sha256:" + "1" * 64)
    assert registry.get_state(approval.approval_id).status == ActionApprovalStatus.EXPIRED


def test_approval_binds_the_container_resource_profile() -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    approval = registry.prepare(
        "s", "e", ids, "sha256:" + "1" * 64, {"memory_mb": 256, "network": "none"}
    )
    registry.approve(approval.approval_id)
    with pytest.raises(ApprovalError, match="command identity changed"):
        registry.consume(
            approval.approval_id,
            "s",
            "e",
            ids,
            "sha256:" + "1" * 64,
            {"memory_mb": 512, "network": "none"},
        )


def test_approval_rejects_mutated_pytest_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    approval = registry.prepare("s", "e", ids, "sha256:" + "1" * 64)
    registry.approve(approval.approval_id)
    original = deterministic_pytest_command

    def mutated(node_ids, plugin_name=None):
        return [*original(node_ids, plugin_name), "--ignore=tests"]

    monkeypatch.setattr("backend.app.test_execution.deterministic_pytest_command", mutated)
    with pytest.raises(ApprovalError, match="command identity changed"):
        registry.consume(
            approval.approval_id,
            "s",
            "e",
            ids,
            "sha256:" + "1" * 64,
        )


def test_approval_binds_supervisor_implementation() -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    profile: dict[str, object] = {"supervisor_sha256": "a" * 64}
    approval = registry.prepare("s", "e", ids, "sha256:" + "1" * 64, execution_profile=profile)
    registry.approve(approval.approval_id)
    with pytest.raises(ApprovalError, match="command identity changed"):
        registry.consume(
            approval.approval_id,
            "s",
            "e",
            ids,
            "sha256:" + "1" * 64,
            execution_profile={"supervisor_sha256": "b" * 64},
        )


def test_approval_can_be_revoked_before_consumption() -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    ids, _ = registry.register_collection("s", "e", ["tests/test_x.py::test_x"])
    approval = registry.prepare("s", "e", ids, "sha256:" + "1" * 64)
    assert registry.revoke(approval.approval_id).status == ActionApprovalStatus.REVOKED
    with pytest.raises(ApprovalError, match="valid user approval"):
        registry.consume(approval.approval_id, "s", "e", ids, "sha256:" + "1" * 64)


def test_concurrent_execution_attempts_consume_and_persist_once() -> None:
    entered_executor = threading.Event()
    allow_executor_to_finish = threading.Event()
    executor_calls: list[str] = []
    persisted_runs: list[str] = []
    calls_lock = threading.Lock()

    class ControlledExecutor:
        def execution_profile(self) -> dict[str, object]:
            return {"network": "none", "timeout_seconds": 5}

        def collect(self, snapshot_root, snapshot_id, runtime_environment_id, **kwargs):
            return RuntimeResult(
                run_id="collect_run",
                snapshot_id=snapshot_id,
                runtime_environment_id=runtime_environment_id,
                status=RuntimeRunStatus.COMPLETED,
                collected_node_ids=["tests/test_x.py::test_x"],
                environment={
                    "python_version": "3.11.13",
                    "platform": "linux",
                    "python_executable_hash": "python-hash",
                    "sys_path_hash": "path-hash",
                    "installed_distributions_hash": "deps-hash",
                },
                started_at=time.time(),
                ended_at=time.time(),
            )

        def execute(
            self,
            snapshot_root,
            snapshot_id,
            runtime_environment_id,
            node_ids,
            run_id,
            **kwargs,
        ):
            with calls_lock:
                executor_calls.append(run_id)
            entered_executor.set()
            if not allow_executor_to_finish.wait(timeout=5):
                raise AssertionError("controlled executor was not released")
            return RuntimeResult(
                run_id=run_id,
                snapshot_id=snapshot_id,
                runtime_environment_id=runtime_environment_id,
                status=RuntimeRunStatus.COMPLETED,
                collected_node_ids=list(node_ids),
                passed=1,
                started_at=time.time(),
                ended_at=time.time(),
            )

        def cancel(self, run_id: str) -> bool:
            return False

    service = RuntimeActionService(
        ControlledExecutor(),
        persist_result=lambda _env, result, *_rest: persisted_runs.append(result.run_id),
    )
    materialization_id = "rtree_fixture"
    snapshot_id = "snap_fixture"
    collection = service.collect(
        snapshot_id,
        materialization_id,
        Path("/immutable/snapshot"),
        "sha256:" + "1" * 64,
    )
    persisted_runs.clear()
    test_ids = [item.test_invocation_id for item in collection.test_invocations]
    prepared = service.prepare(
        snapshot_id, collection.runtime_environment.runtime_environment_id, test_ids
    )
    approved = service.approve(prepared.approval_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.execute, approved.approval_id)
        assert entered_executor.wait(timeout=5)
        second = pool.submit(service.execute, approved.approval_id)
        with pytest.raises(ApprovalError, match="user approval"):
            second.result(timeout=5)
        allow_executor_to_finish.set()
        result = first.result(timeout=5)

    assert result.status == RuntimeRunStatus.COMPLETED
    assert executor_calls == [approved.run_id]
    assert persisted_runs == [approved.run_id]
    assert service.approval_state(approved.approval_id).status == ActionApprovalStatus.CONSUMED


def test_execution_failure_does_not_restore_consumed_approval() -> None:
    class FailingExecutor:
        def execution_profile(self) -> dict[str, object]:
            return {"network": "none"}

        def collect(self, snapshot_root, snapshot_id, runtime_environment_id, **kwargs):
            return RuntimeResult(
                run_id="collect_run",
                snapshot_id=snapshot_id,
                runtime_environment_id=runtime_environment_id,
                status=RuntimeRunStatus.COMPLETED,
                collected_node_ids=["tests/test_x.py::test_x"],
                started_at=time.time(),
                ended_at=time.time(),
            )

        def execute(self, *args, **kwargs):
            raise RuntimeExecutionError("container failed")

        def cancel(self, run_id: str) -> bool:
            return False

    service = RuntimeActionService(FailingExecutor())
    collection = service.collect(
        "snap_failure", "rtree_failure", Path("/immutable/snapshot"), "sha256:" + "2" * 64
    )
    prepared = service.prepare(
        "snap_failure",
        collection.runtime_environment.runtime_environment_id,
        [item.test_invocation_id for item in collection.test_invocations],
    )
    service.approve(prepared.approval_id)
    with pytest.raises(RuntimeExecutionError, match="container failed"):
        service.execute(prepared.approval_id)
    assert service.approval_state(prepared.approval_id).status == ActionApprovalStatus.CONSUMED
    with pytest.raises(ApprovalError, match="user approval"):
        service.execute(prepared.approval_id)


def test_runtime_action_api_rejects_shell_fields_empty_and_oversized_test_sets() -> None:
    base = {
        "snapshot_id": "snap_fixture",
        "runtime_environment_id": "runtime_fixture",
        "test_ids": ["invocation_1"],
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RuntimeActionRequest.model_validate(base | {"command": "pytest tests"})
    with pytest.raises(ValidationError):
        RuntimeActionRequest.model_validate(base | {"test_ids": []})
    with pytest.raises(ValidationError):
        RuntimeActionRequest.model_validate(
            base | {"test_ids": [f"invocation_{index}" for index in range(501)]}
        )


def test_approved_pytest_argv_is_deterministic_and_binds_selected_nodes() -> None:
    nodes = ["tests/test_space.py::test_case[with spaces]"]
    first = deterministic_pytest_command(nodes)
    second = deterministic_pytest_command(nodes)
    assert first == second
    assert first[:2] == ["python", "-m"]
    assert first[-1] == nodes[0]
    assert "sh" not in first
    assert command_identity_hash("s", "e", ["i"], nodes, "image") != (
        command_identity_hash("s", "e", ["i"], ["tests/test_space.py::other"], "image")
    )


def test_approved_pytest_argv_has_a_bounded_argument_size() -> None:
    with pytest.raises(ApprovalError, match="64 KiB argument limit"):
        deterministic_pytest_command(["tests/test.py::" + "a" * (64 * 1024)])


def test_approval_bounds_selected_test_count() -> None:
    registry = ApprovalRegistry(ttl_seconds=30)
    too_many = [f"tests/test_{index}.py::test_case" for index in range(501)]
    registry.register_collection("s", "e", too_many)
    with pytest.raises(ApprovalError, match="no more than 500"):
        registry.prepare("s", "e", too_many, "sha256:" + "1" * 64)


def test_backend_command_keeps_shell_looking_item_as_single_argv(tmp_path: Path) -> None:
    executor = DockerTestExecutor(Settings(runtime_container_image="sha256:" + "1" * 64))
    command = executor._container_argv(
        "sha256:" + "1" * 64,
        "codegraph-test-fixture",
        tmp_path,
        "/plugin.py",
        "plugin",
        "execute",
        ["tests/test_x.py::test_x"],
        tmp_path / "source-map.json",
    )
    assert command[-2:] == ["--", "tests/test_x.py::test_x"]
    assert "sh" not in command
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert command[command.index("--entrypoint") + 1] == "python"
    assert not any("dst=/result.json" in item for item in command)
    assert any("dst=/runtime-supervisor.py,readonly" in item for item in command)
    node_id = "tests/test_x.py::test_x; touch /tmp/pwned"
    assert validate_node_id(node_id) == node_id
    safe_command = deterministic_pytest_command([node_id])
    assert safe_command[-2:] == ["--", node_id]


@pytest.mark.parametrize(
    "node_id",
    [
        "../outside/test_escape.py::test_x",
        "/tmp/test_escape.py::test_x",
        "tests/test_x.py;touch /tmp/pwned::test_x",
        "tests/test_x.txt::test_x",
        "tests/test_x.py",
    ],
)
def test_allowlist_rejects_unsafe_pytest_node_ids(node_id: str) -> None:
    with pytest.raises(ApprovalError):
        validate_node_id(node_id)


def test_runtime_collection_captures_full_tree_and_dirty_snapshot_content(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {
            "tests/test_asset.py": (
                "from pathlib import Path\n"
                "def test_asset():\n"
                "    assert Path('fixtures/payload.txt').read_text() == 'snapshot-v1'\n"
            ),
            "fixtures/payload.txt": "snapshot-v1",
            "templates/page.html": "<h1>tracked</h1>",
        },
    )
    (repo / "fixtures/payload.txt").write_text("dirty-v2", encoding="utf-8")
    materialization = capture_runtime_materialization(repo, tmp_path / "cache", "snap_" + "b" * 64)
    root = runtime_files_root(
        tmp_path / "cache", materialization.snapshot_id, materialization.materialization_id
    )
    assert (root / "fixtures/payload.txt").read_text(encoding="utf-8") == "dirty-v2"
    assert (root / "templates/page.html").exists()
    assert "fixtures/payload.txt" in {entry.path for entry in materialization.files}


def test_runtime_assets_do_not_become_graph_inputs_or_change_graph_snapshot_identity(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {
            "tests/test_asset.py": "def test_asset(): pass\n",
            "fixtures/payload.bin": "before",
        },
    )
    settings = Settings(allowed_repository_roots=[tmp_path], snapshot_cache_dir=tmp_path / "cache")
    original_id, _ = identify_snapshot(repo, settings)
    root, _repository_info = inspect_repository(repo, settings)
    indexed_paths, _ = _discover(root, include_untracked=False)
    manifest, _sources, _materialized, _diagnostics = _collect_manifest(
        root, indexed_paths, settings
    )
    assert "fixtures/payload.bin" not in {entry.path for entry in manifest}

    first = capture_runtime_materialization(repo, settings.snapshot_cache_dir, original_id)
    (repo / "fixtures/payload.bin").write_text("after", encoding="utf-8")
    second = capture_runtime_materialization(repo, settings.snapshot_cache_dir, original_id)
    updated_id, _ = identify_snapshot(repo, settings)
    assert updated_id == original_id
    assert first.materialization_id != second.materialization_id


def test_untracked_runtime_files_follow_existing_include_policy(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path, {"tests/test_x.py": "def test_x(): pass\n", ".gitignore": "ignored.txt\n"}
    )
    (repo / "untracked.json").write_text("{}", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret", encoding="utf-8")
    default = capture_runtime_materialization(repo, tmp_path / "cache", "snap_" + "c" * 64)
    included = capture_runtime_materialization(
        repo, tmp_path / "cache", "snap_" + "c" * 64, include_untracked=True
    )
    assert "untracked.json" not in {entry.path for entry in default.files}
    assert "untracked.json" in {entry.path for entry in included.files}
    assert "ignored.txt" not in {entry.path for entry in included.files}


def test_symlink_escape_is_rejected_and_git_internals_are_never_materialized(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"tests/test_x.py": "def test_x(): pass\n"})
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (repo / "escape.txt").symlink_to(outside)
    _git(repo, "add", "escape.txt")
    materialization = capture_runtime_materialization(repo, tmp_path / "cache", "snap_" + "d" * 64)
    assert "escape.txt" not in {entry.path for entry in materialization.files}
    assert not (
        runtime_files_root(
            tmp_path / "cache", materialization.snapshot_id, materialization.materialization_id
        )
        / "escape.txt"
    ).exists()


def test_real_container_runs_against_immutable_assets_and_has_no_live_fallback(
    tmp_path: Path, runtime_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {
            "tests/test_asset.py": (
                "import os\nfrom pathlib import Path\n"
                "def test_snapshot_assets():\n"
                "    assert Path('fixtures/value.txt').read_text() == 'snapshot-value'\n"
                "    assert not Path('fixtures/late.txt').exists()\n"
                "    assert os.environ.get('CODEGRAPH_TEST_HOST_SECRET') is None\n"
                "    Path('/tmp/runtime-write.txt').write_text('temporary')\n"
            ),
            "fixtures/value.txt": "snapshot-value",
        },
    )
    monkeypatch.setenv("CODEGRAPH_TEST_HOST_SECRET", "must-not-enter-container")
    (repo / "fixtures/value.txt").write_text("live-working-tree-value", encoding="utf-8")
    (repo / "fixtures/late.txt").write_text("not-in-snapshot", encoding="utf-8")
    result = _executor(runtime_image).execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_asset.py::test_snapshot_assets"],
        "run_asset_snapshot",
    )
    assert result.status == RuntimeRunStatus.COMPLETED
    assert result.passed == 1
    assert result.environment["python_version"]
    assert (root / "fixtures/value.txt").read_text(encoding="utf-8") == "snapshot-value"
    assert not (root / "fixtures/late.txt").exists()
    assert (
        runtime_files_root(cache, materialization.snapshot_id, materialization.materialization_id)
        == root
    )
    _assert_no_runtime_containers()


@pytest.mark.parametrize("mutation", ["content", "extra"])
def test_runtime_refuses_materialization_changes_after_collection(
    tmp_path: Path, mutation: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path, "sha256:" + "1" * 64, {"tests/test_x.py": "def test_x(): pass\n"}
    )
    if mutation == "content":
        test_file = root / "tests/test_x.py"
        test_file.chmod(0o644)
        test_file.write_text("def test_x(): assert False\n", encoding="utf-8")
    else:
        (root / "tests/unlisted.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
    with pytest.raises(RuntimeMaterializationError):
        verify_runtime_files_root(
            root, materialization.snapshot_id, materialization.materialization_id
        )


@pytest.mark.parametrize(
    ("initial_executable", "changed_executable"), [(False, True), (True, False)]
)
def test_runtime_materialization_binds_both_executable_mode_directions(
    tmp_path: Path, initial_executable: bool, changed_executable: bool
) -> None:
    repo = _repository(tmp_path, {"tools/helper.py": "print('helper')\n"})
    if initial_executable:
        (repo / "tools/helper.py").chmod(0o755)
    materialization = capture_runtime_materialization(repo, tmp_path / "cache", "snap_" + "e" * 64)
    root = runtime_files_root(
        tmp_path / "cache", materialization.snapshot_id, materialization.materialization_id
    )
    helper = root / "tools/helper.py"
    entry = next(item for item in materialization.files if item.path == "tools/helper.py")
    assert entry.executable is initial_executable
    assert verify_runtime_files_root(root, materialization.snapshot_id).materialization_id == (
        materialization.materialization_id
    )
    helper.chmod(0o555 if changed_executable else 0o444)
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == entry.content_hash
    with pytest.raises(RuntimeMaterializationError, match="executable mode"):
        verify_runtime_files_root(
            root, materialization.snapshot_id, materialization.materialization_id
        )


def test_runtime_materialization_rejects_symlink_substitution(tmp_path: Path) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path, "sha256:" + "1" * 64, {"tests/test_x.py": "def test_x(): pass\n"}
    )
    outside = tmp_path / "outside.py"
    outside.write_text("def test_x(): pass\n", encoding="utf-8")
    target = root / "tests/test_x.py"
    target.chmod(0o644)
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(RuntimeMaterializationError):
        verify_runtime_files_root(
            root, materialization.snapshot_id, materialization.materialization_id
        )


@pytest.mark.parametrize("mode", ["collect", "execute"])
def test_corrupt_mode_stops_collection_and_execution_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path, "sha256:" + "1" * 64, {"tests/test_x.py": "def test_x(): pass\n"}
    )
    test_file = root / "tests/test_x.py"
    test_file.chmod(0o555)
    executor = _executor("sha256:" + "1" * 64)
    docker_checks = 0

    def ensure_available() -> str:
        nonlocal docker_checks
        docker_checks += 1
        return "sha256:" + "1" * 64

    monkeypatch.setattr(executor, "ensure_available", ensure_available)
    with pytest.raises(RuntimeMaterializationError, match="executable mode"):
        if mode == "collect":
            executor.collect(
                root,
                materialization.snapshot_id,
                "runtime_fixture",
                materialization_id=materialization.materialization_id,
            )
        else:
            executor.execute(
                root,
                materialization.snapshot_id,
                "runtime_fixture",
                ["tests/test_x.py::test_x"],
                "run_corrupt_mode",
                materialization_id=materialization.materialization_id,
            )
    assert docker_checks == 0


def test_materialization_is_rechecked_after_setup_before_docker_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path, "sha256:" + "1" * 64, {"tests/test_x.py": "def test_x(): pass\n"}
    )
    executor = _executor("sha256:" + "1" * 64)
    monkeypatch.setattr(executor, "ensure_available", lambda: "sha256:" + "1" * 64)
    original_argv = executor._container_argv

    def mutate_after_initial_verification(*args, **kwargs):
        command = original_argv(*args, **kwargs)
        (root / "tests/test_x.py").chmod(0o555)
        return command

    launched = False

    def fake_popen(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("Docker must not start with a changed materialization")

    monkeypatch.setattr(executor, "_container_argv", mutate_after_initial_verification)
    monkeypatch.setattr("backend.app.test_execution.subprocess.Popen", fake_popen)
    with pytest.raises(RuntimeMaterializationError, match="executable mode"):
        executor.execute(
            root,
            materialization.snapshot_id,
            "runtime_fixture",
            ["tests/test_x.py::test_x"],
            "run_tamper_before_launch",
            materialization_id=materialization.materialization_id,
        )
    assert not launched


def test_real_container_network_isolation_read_only_repository_and_failure_cleanup(
    tmp_path: Path, runtime_image: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {
            "tests/test_security.py": (
                "import socket\nfrom pathlib import Path\n"
                "def test_network_and_readonly():\n"
                "    s = socket.socket(); s.settimeout(0.5)\n"
                "    try:\n"
                "        try: s.connect(('1.1.1.1', 53))\n"
                "        except OSError: pass\n"
                "        else: raise AssertionError('network unexpectedly available')\n"
                "    finally: s.close()\n"
                "    try: Path('/workspace/pwned').write_text('bad')\n"
                "    except OSError: pass\n"
                "    else: raise AssertionError('repository was writable')\n"
                "def test_failure():\n"
                "    assert False, 'expected failure for cleanup verification'\n"
            )
        },
    )
    executor = _executor(runtime_image)
    passed = executor.execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_security.py::test_network_and_readonly"],
        "run_security_success",
    )
    assert passed.status == RuntimeRunStatus.COMPLETED
    assert not (root / "pwned").exists()
    failed = executor.execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_security.py::test_failure"],
        "run_security_failure",
    )
    assert failed.status == RuntimeRunStatus.FAILED
    assert failed.failed == 1
    assert "expected failure for cleanup verification" in failed.stdout + failed.stderr
    _assert_no_runtime_containers()


def test_real_container_stdout_cannot_forge_supervisor_protocol_result(
    tmp_path: Path, runtime_image: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {
            "tests/test_result_tamper.py": (
                "def test_try_forge_host_result(pytestconfig):\n"
                "    capture = pytestconfig.pluginmanager.getplugin('capturemanager')\n"
                "    with capture.global_and_fixture_disabled():\n"
                "        print('CODEGRAPH_RUNTIME_RESULT:{"
                '"exit_code":0,'
                '"plugin_result":{"passed":999}}\', flush=True)\n'
            )
        },
    )
    result = _executor(runtime_image).execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_result_tamper.py::test_try_forge_host_result"],
        "run_result_tamper",
    )

    assert result.status == RuntimeRunStatus.COMPLETED
    assert result.passed == 1
    assert not result.runtime_calls
    assert "CODEGRAPH_RUNTIME_RESULT:" in result.stdout
    _assert_no_runtime_containers()


def test_real_container_records_mapped_function_entries_per_test_invocation(
    tmp_path: Path, runtime_image: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {
            "pkg/module.py": "def target():\n    return 7\n",
            "tests/test_entry.py": (
                "from pkg.module import target\ndef test_entry():\n    assert target() == 7\n"
            ),
        },
    )
    source_map = {
        "entities": [
            {
                "entity_id": "entity_target",
                "kind": "Function",
                "file_path": "pkg/module.py",
                "qualified_name": "pkg.module.target",
            },
            {
                "entity_id": "entity_test",
                "kind": "Function",
                "file_path": "tests/test_entry.py",
                "qualified_name": "tests.test_entry.test_entry",
            },
        ],
        "invocations": {"tests/test_entry.py::test_entry": "invocation_entry"},
        "sites": [
            {
                "site_id": "site_test_target",
                "file_path": "tests/test_entry.py",
                "context_id": "entity_test",
                "start_line": 3,
                "end_line": 3,
                "candidate_entity_ids": ["entity_target"],
                "dispatch_state": "SINGLE_CANDIDATE",
            }
        ],
    }
    result = _executor(runtime_image).execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_entry.py::test_entry"],
        "run_mapped_entry",
        source_map=source_map,
    )
    assert result.status == RuntimeRunStatus.COMPLETED
    mapped = [
        entry for entry in result.runtime_entries if entry.get("entity_id") == "entity_target"
    ]
    assert len(mapped) == 1
    assert mapped[0]["context"] == "TEST_INVOCATION"
    assert mapped[0]["test_invocation_id"] == "invocation_entry"
    mapped_calls = [
        call for call in result.runtime_calls if call.get("site_id") == "site_test_target"
    ]
    assert len(mapped_calls) == 1
    assert mapped_calls[0]["source_entity_id"] == "entity_test"
    assert mapped_calls[0]["target_entity_id"] == "entity_target"


def test_real_container_enforces_limits_and_cancellation_cleans_up(
    tmp_path: Path, runtime_image: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {"tests/test_slow.py": "import time\ndef test_slow():\n    time.sleep(60)\n"},
    )
    executor = _executor(runtime_image, timeout=60)
    holder: dict[str, object] = {}

    def run() -> None:
        holder["result"] = executor.execute(
            root,
            materialization.snapshot_id,
            "runtime_fixture",
            ["tests/test_slow.py::test_slow"],
            "run_security_cancel",
        )

    worker = threading.Thread(target=run)
    worker.start()
    container = None
    deadline = time.monotonic() + 15
    inspection: dict[str, object] | None = None
    while time.monotonic() < deadline:
        container = executor.active_container_name("run_security_cancel")
        if container:
            inspected = subprocess.run(
                ["docker", "inspect", container], capture_output=True, text=True, check=False
            )
            if inspected.returncode == 0:
                inspection = json.loads(inspected.stdout)[0]
                break
        time.sleep(0.1)
    try:
        assert inspection is not None
        config = inspection["Config"]
        host = inspection["HostConfig"]
        assert host["ReadonlyRootfs"] is True
        assert host["NetworkMode"] == "none"
        assert host["Privileged"] is False
        assert "ALL" in host["CapDrop"]
        assert host["Memory"] == 256 * 1024 * 1024
        assert host["PidsLimit"] == 64
        assert host["NanoCpus"] == 500_000_000
        assert "/tmp" in host["Tmpfs"]
        assert "noexec" in host["Tmpfs"]["/tmp"]
        workspace = next(
            mount for mount in inspection["Mounts"] if mount["Destination"] == "/workspace"
        )
        assert workspace["RW"] is False
        assert all("CODEGRAPH_TEST_HOST_SECRET" not in variable for variable in config["Env"])
    finally:
        executor.cancel("run_security_cancel")
        worker.join(timeout=10)
    assert not worker.is_alive()
    result = holder["result"]
    assert result.status == RuntimeRunStatus.CANCELLED
    _assert_no_runtime_containers()


def test_real_container_timeout_terminates_and_removes_container(
    tmp_path: Path, runtime_image: str
) -> None:
    _repo, _cache, materialization, root = _snapshot(
        tmp_path,
        runtime_image,
        {"tests/test_slow.py": "import time\ndef test_slow():\n    time.sleep(30)\n"},
    )
    result = _executor(runtime_image, timeout=2).execute(
        root,
        materialization.snapshot_id,
        "runtime_fixture",
        ["tests/test_slow.py::test_slow"],
        "run_security_timeout",
    )
    assert result.status == RuntimeRunStatus.TIMED_OUT
    _assert_no_runtime_containers()


def test_startup_cleanup_removes_only_confirmed_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor("sha256:" + "1" * 64)
    monkeypatch.setattr("backend.app.test_execution.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(executor, "_resolve_local_docker_endpoint", lambda _cli: "unix:///local")
    monkeypatch.setattr(
        executor,
        "_process_start_marker",
        lambda pid: {100: "active-owner", 200: ""}.get(pid),
    )
    labels = {
        "orphan": {
            "com.falkordb-codegraph.runtime": "true",
            "com.falkordb-codegraph.owner-pid": "200",
            "com.falkordb-codegraph.owner-started-at": "old-owner",
        },
        "active": {
            "com.falkordb-codegraph.runtime": "true",
            "com.falkordb-codegraph.owner-pid": "100",
            "com.falkordb-codegraph.owner-started-at": "active-owner",
        },
        "unknown": {"com.falkordb-codegraph.runtime": "true"},
    }
    removed: list[str] = []

    def fake_run(command, **kwargs):
        if command[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(command, 0, "orphan\nactive\nunknown\n", "")
        if command[1] == "inspect":
            container_id = command[-1]
            return subprocess.CompletedProcess(command, 0, json.dumps(labels[container_id]), "")
        if command[1:3] == ["rm", "-f"]:
            removed.append(command[-1])
            return subprocess.CompletedProcess(command, 0, "removed", "")
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", fake_run)

    assert executor.cleanup_orphaned_containers() == ["orphan"]
    assert removed == ["orphan"]


def test_concurrent_startup_cleanup_is_idempotent_and_preserves_live_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.test_execution.shutil.which", lambda _name: "docker")
    executors = [_executor("sha256:" + "1" * 64) for _ in range(2)]
    state_lock = threading.Lock()
    containers = {
        "orphan": {
            "com.falkordb-codegraph.runtime": "true",
            "com.falkordb-codegraph.owner-pid": "200",
            "com.falkordb-codegraph.owner-started-at": "old-owner",
        },
        "active": {
            "com.falkordb-codegraph.runtime": "true",
            "com.falkordb-codegraph.owner-pid": "100",
            "com.falkordb-codegraph.owner-started-at": "active-owner",
        },
    }

    def fake_run(command, **kwargs):
        if command[1:3] == ["ps", "-aq"]:
            with state_lock:
                current = "\n".join(containers)
            return subprocess.CompletedProcess(command, 0, current, "")
        if command[1] == "inspect":
            with state_lock:
                labels = containers.get(command[-1])
            if labels is None:
                return subprocess.CompletedProcess(command, 1, "", "No such object")
            return subprocess.CompletedProcess(command, 0, json.dumps(labels), "")
        if command[1:3] == ["rm", "-f"]:
            with state_lock:
                containers.pop(command[-1], None)
            return subprocess.CompletedProcess(command, 0, "removed", "")
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", fake_run)
    for executor in executors:
        monkeypatch.setattr(
            executor, "_resolve_local_docker_endpoint", lambda _cli: "unix:///local"
        )
        monkeypatch.setattr(
            executor,
            "_process_start_marker",
            lambda pid: {100: "active-owner", 200: ""}.get(pid),
        )
    start = threading.Barrier(2)

    def cleanup(executor: DockerTestExecutor) -> list[str]:
        start.wait(timeout=5)
        return executor.cleanup_orphaned_containers()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cleanup, item) for item in executors]
        results = [future.result(timeout=10) for future in futures]

    assert set(containers) == {"active"}
    assert all("active" not in result for result in results)
    assert executors[0].cleanup_orphaned_containers() == []


def test_real_docker_startup_cleanup_removes_orphaned_runtime_container(
    runtime_image: str,
) -> None:
    name = f"codegraph-test-orphan-probe-{uuid.uuid4().hex}"
    created = subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "com.falkordb-codegraph.runtime=true",
            "--label",
            f"com.falkordb-codegraph.owner-pid={os.getpid()}",
            "--label",
            "com.falkordb-codegraph.owner-started-at=previous-owner-instance",
            runtime_image,
            "python",
            "-c",
            "pass",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    removed = _executor(runtime_image).cleanup_orphaned_containers()
    assert any(created.startswith(container_id) for container_id in removed)
    inspected = subprocess.run(["docker", "inspect", created], capture_output=True, check=False)
    assert inspected.returncode != 0


def test_docker_unavailable_fails_closed_without_host_fallback() -> None:
    executor = DockerTestExecutor(
        Settings(
            runtime_docker_bin="/missing/codegraph-docker",
            runtime_container_image="sha256:" + "1" * 64,
        )
    )
    with pytest.raises(DockerUnavailable, match="not installed or not on PATH"):
        executor.ensure_available()


def test_container_cleanup_retries_transient_daemon_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor("sha256:" + "1" * 64)
    attempts = 0

    def fake_run(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return subprocess.CompletedProcess(args[0], 1, b"", b"daemon busy")
        return subprocess.CompletedProcess(args[0], 0, b"removed", b"")

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", fake_run)
    monkeypatch.setattr("backend.app.test_execution.time.sleep", lambda _seconds: None)

    assert executor._kill_container("codegraph-test-retry") is True
    assert attempts == 3
    assert "codegraph-test-retry" not in executor._cleanup_failures


def test_container_cleanup_confirms_removal_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor("sha256:" + "1" * 64)
    calls = 0

    def remove_then_inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                args[0],
                1,
                b"",
                b"Error response from daemon: removal of container is already in progress",
            )
        return subprocess.CompletedProcess(
            args[0], 1, b"", b"Error: No such container: codegraph-test-removing"
        )

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", remove_then_inspect)
    assert executor._kill_container("codegraph-test-removing") is True
    assert calls == 2
    assert "codegraph-test-removing" not in executor._cleanup_failures


def test_container_cleanup_failure_is_reported_and_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor("sha256:" + "1" * 64)

    def fail_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, b"", b"daemon unavailable")

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", fail_run)
    monkeypatch.setattr("backend.app.test_execution.time.sleep", lambda _seconds: None)

    assert executor._kill_container("codegraph-test-stuck") is False
    assert executor._cleanup_failures["codegraph-test-stuck"] == "daemon unavailable"

    def succeed_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"removed", b"")

    monkeypatch.setattr("backend.app.test_execution.subprocess.run", succeed_run)
    assert executor._kill_container("codegraph-test-stuck") is True
    assert "codegraph-test-stuck" not in executor._cleanup_failures

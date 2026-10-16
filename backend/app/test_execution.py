from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings
from .ids import stable_id
from .lifecycle import register_active_snapshot, snapshot_lifecycle_lock, unregister_active_snapshot
from .materialization import verify_runtime_files_root
from .models import (
    ActionApproval,
    ActionApprovalStatus,
    RuntimeCollection,
    RuntimeEnvironment,
    RuntimeRunStatus,
    SnapshotAnalysis,
    TestDefinition,
    TestExecutionResult,
    TestInvocation,
)
from .runtime import identify_pytest_items
from .runtime_validation import validate_runtime_calls

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_NODE_ID_PATH = re.compile(r"^[-A-Za-z0-9_./:\[\]@=,+ ]+$")
MAX_APPROVED_TESTS = 500
MAX_APPROVED_COMMAND_BYTES = 64 * 1024
_PLUGIN_SOURCE = Path(__file__).with_name("runtime_pytest_plugin.py")
_SUPERVISOR_SOURCE = Path(__file__).with_name("runtime_supervisor.py")
logger = logging.getLogger(__name__)


def _runtime_source_map(
    analysis: SnapshotAnalysis | None, invocation_ids: dict[str, str] | None = None
) -> dict[str, object]:
    if analysis is None:
        return {"entities": [], "invocations": invocation_ids or {}, "sites": []}
    entities = [
        {
            "entity_id": entity.entity_id,
            "kind": entity.kind,
            "file_path": entity.file_path,
            "qualified_name": entity.qualified_name,
        }
        for entity in analysis.entities
    ]
    dispatch = {state.site_id: state for state in analysis.dispatch}
    sites = [
        {
            "site_id": site.site_id,
            "file_path": site.file_path,
            "context_id": site.context_id,
            "start_line": site.span.start_line,
            "end_line": site.span.end_line,
            "candidate_entity_ids": dispatch[site.site_id].candidate_entity_ids
            if site.site_id in dispatch
            else [],
            "dispatch_state": dispatch[site.site_id].state.value
            if site.site_id in dispatch
            else "UNKNOWN",
        }
        for site in analysis.sites
        if site.kind.value == "CALL_CALLEE"
    ]
    return {"entities": entities, "invocations": invocation_ids or {}, "sites": sites}


class DockerUnavailable(RuntimeError):
    """Docker or the explicitly pinned Python image cannot be used."""


class ApprovalError(ValueError):
    pass


class RuntimeExecutionError(RuntimeError):
    pass


class RuntimeCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedAction:
    snapshot_id: str
    runtime_environment_id: str
    test_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    command_hash: str


class ApprovalRegistry:
    """Single-process, single-use approvals bound to collected test identities."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._collections: dict[tuple[str, str], dict[str, str]] = {}
        self._approvals: dict[str, ActionApproval] = {}

    def register_collection(
        self, snapshot_id: str, runtime_environment_id: str, node_ids: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        safe_node_ids = [validate_node_id(node_id) for node_id in node_ids]
        definitions, invocations = identify_pytest_items(snapshot_id, safe_node_ids)
        lookup = {item.test_invocation_id: item.node_id for item in invocations}
        with self._lock:
            self._collections[(snapshot_id, runtime_environment_id)] = lookup
        return list(lookup), [item.node_id for item in invocations]

    def prepare(
        self,
        snapshot_id: str,
        runtime_environment_id: str,
        test_ids: Sequence[str],
        image_digest: str,
        execution_profile: dict[str, object] | None = None,
    ) -> ActionApproval:
        if len(test_ids) > MAX_APPROVED_TESTS:
            raise ApprovalError(f"Select no more than {MAX_APPROVED_TESTS} tests per run")
        ids = tuple(sorted(set(test_ids)))
        if not ids or len(ids) != len(test_ids):
            raise ApprovalError("Select one or more unique collected test invocation IDs")
        with self._lock:
            collection = self._collections.get((snapshot_id, runtime_environment_id))
            if collection is None:
                raise ApprovalError("Collect tests for this snapshot and environment first")
            if any(test_id not in collection for test_id in ids):
                raise ApprovalError("Approval includes a test that was not collected")
            node_ids = [collection[test_id] for test_id in ids]
            command_hash = command_identity_hash(
                snapshot_id,
                runtime_environment_id,
                ids,
                node_ids,
                image_digest,
                execution_profile,
            )
            now = time.time()
            approval = ActionApproval(
                approval_id=stable_id(uuid.uuid4().hex, prefix="approval_"),
                snapshot_id=snapshot_id,
                runtime_environment_id=runtime_environment_id,
                run_id=stable_id(uuid.uuid4().hex, prefix="run_"),
                test_ids=list(ids),
                pytest_command=deterministic_pytest_command(node_ids),
                exact_command_hash=command_hash,
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._approvals[approval.approval_id] = approval
            return approval.model_copy(deep=True)

    def approve(self, approval_id: str) -> ActionApproval:
        with self._lock:
            approval = self._get_live(approval_id)
            if approval.status != ActionApprovalStatus.PENDING:
                raise ApprovalError("Approval is not pending user approval")
            updated = approval.model_copy(update={"status": ActionApprovalStatus.APPROVED})
            self._approvals[approval_id] = updated
            return updated.model_copy(deep=True)

    def revoke(self, approval_id: str) -> ActionApproval:
        with self._lock:
            approval = self._get_live(approval_id)
            if approval.status not in {
                ActionApprovalStatus.PENDING,
                ActionApprovalStatus.APPROVED,
            }:
                raise ApprovalError("This approval can no longer be revoked")
            updated = approval.model_copy(update={"status": ActionApprovalStatus.REVOKED})
            self._approvals[approval_id] = updated
            return updated.model_copy(deep=True)

    def get(self, approval_id: str) -> ActionApproval:
        with self._lock:
            approval = self._get_live(approval_id)
            return approval.model_copy(deep=True)

    def get_state(self, approval_id: str) -> ActionApproval:
        """Return authoritative state, recording expiry without making it executable."""
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise ApprovalError("Approval not found")
            if approval.expires_at <= time.time() and approval.status in {
                ActionApprovalStatus.PENDING,
                ActionApprovalStatus.APPROVED,
            }:
                approval = approval.model_copy(update={"status": ActionApprovalStatus.EXPIRED})
                self._approvals[approval_id] = approval
            return approval.model_copy(deep=True)

    def consume(
        self,
        approval_id: str,
        snapshot_id: str,
        runtime_environment_id: str,
        test_ids: Sequence[str],
        image_digest: str,
        execution_profile: dict[str, object] | None = None,
    ) -> tuple[ActionApproval, tuple[str, ...]]:
        with self._lock:
            approval = self._get_live(approval_id)
            if approval.status != ActionApprovalStatus.APPROVED:
                raise ApprovalError("A valid user approval is required before execution")
            ids = tuple(sorted(set(test_ids)))
            if (
                snapshot_id != approval.snapshot_id
                or runtime_environment_id != approval.runtime_environment_id
                or list(ids) != approval.test_ids
            ):
                raise ApprovalError(
                    "Execution action differs from the approved snapshot or test set"
                )
            collection = self._collections.get((snapshot_id, runtime_environment_id), {})
            try:
                node_ids = tuple(collection[test_id] for test_id in ids)
            except KeyError as exc:
                raise ApprovalError("Approved test collection is no longer available") from exc
            actual_hash = command_identity_hash(
                snapshot_id,
                runtime_environment_id,
                ids,
                node_ids,
                image_digest,
                execution_profile,
            )
            if actual_hash != approval.exact_command_hash:
                raise ApprovalError("Approved command identity changed; prepare a new approval")
            consumed = approval.model_copy(update={"status": ActionApprovalStatus.CONSUMED})
            self._approvals[approval_id] = consumed
            return consumed.model_copy(deep=True), node_ids

    def _get_live(self, approval_id: str) -> ActionApproval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalError("Approval not found")
        if approval.expires_at <= time.time() and approval.status in {
            ActionApprovalStatus.PENDING,
            ActionApprovalStatus.APPROVED,
        }:
            self._approvals[approval_id] = approval.model_copy(
                update={"status": ActionApprovalStatus.EXPIRED}
            )
            raise ApprovalError("Approval has expired")
        return approval


def command_identity_hash(
    snapshot_id: str,
    runtime_environment_id: str,
    test_ids: Sequence[str],
    node_ids: Sequence[str],
    image_digest: str,
    execution_profile: dict[str, object] | None = None,
) -> str:
    value = {
        "snapshot_id": snapshot_id,
        "runtime_environment_id": runtime_environment_id,
        "test_ids": list(test_ids),
        "node_ids": list(node_ids),
        "image_digest": image_digest,
        "execution_profile": execution_profile or {},
        "command": deterministic_pytest_command(node_ids),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_node_id(node_id: str) -> str:
    file_path, separator, item = node_id.partition("::")
    path = PurePosixPath(file_path)
    if (
        not separator
        or not item
        or not file_path
        or file_path.startswith("-")
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
        or "\x00" in node_id
        or not _NODE_ID_PATH.fullmatch(file_path)
        # Pytest parameter IDs may contain URLs, quoted values, and other punctuation.
        # They are passed as individual argv entries after `--`, never through a shell.
        or not all(character.isprintable() for character in item)
    ):
        raise ApprovalError("Unsafe pytest node ID")
    return node_id


def _runtime_plugin_name() -> str:
    """Use a stable plugin module name so approval previews match execution argv."""
    digest = hashlib.sha256(_PLUGIN_SOURCE.read_bytes()).hexdigest()[:20]
    return f"cg_plugin_{digest}"


def deterministic_pytest_command(
    node_ids: Sequence[str], plugin_name: str | None = None
) -> list[str]:
    """Return the fixed argv shown for approval and passed to the isolated supervisor."""
    command = [
        "python",
        "-m",
        "pytest",
        "-p",
        plugin_name or _runtime_plugin_name(),
        "--rootdir=/workspace",
        "-o",
        "addopts=",
        "--import-mode=importlib",
        "-q",
        "--",
        *(validate_node_id(node_id) for node_id in node_ids),
    ]
    command_size = sum(len(argument.encode("utf-8")) + 1 for argument in command)
    if command_size > MAX_APPROVED_COMMAND_BYTES:
        raise ApprovalError("Approved pytest command exceeds the 64 KiB argument limit")
    return command


class _OutputCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = bytearray()

    def drain(self, stream) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = self.limit - len(self.value)
                if remaining > 0:
                    self.value.extend(chunk[:remaining])
        finally:
            stream.close()


class DockerTestExecutor:
    """Run only fixed pytest collection/test command shapes in disposable containers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._active: dict[str, tuple[str, subprocess.Popen[bytes] | None, threading.Event]] = {}
        self._docker_endpoint: str | None = None
        self._cleanup_failures: dict[str, str] = {}
        self._instance_id = uuid.uuid4().hex
        self._owner_pid = os.getpid()
        self._owner_started_at = self._process_start_marker(self._owner_pid)

    def ensure_available(self) -> str:
        image = self.settings.runtime_container_image
        if not image or not (_IMAGE_ID.fullmatch(image) or _IMAGE_DIGEST.fullmatch(image)):
            raise DockerUnavailable(
                "Test execution is unavailable: configure RUNTIME_CONTAINER_IMAGE to an immutable "
                "Docker image ID or repository@sha256 digest. "
                "Static repository analysis remains available."
            )
        docker_cli = shutil.which(self.settings.runtime_docker_bin)
        if docker_cli is None:
            raise DockerUnavailable(
                "Test execution is unavailable because Docker is not installed or not on PATH. "
                "Static repository analysis remains available."
            )
        self._docker_endpoint = self._resolve_local_docker_endpoint(docker_cli)
        try:
            result = subprocess.run(
                [
                    docker_cli,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    image,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=self._docker_environment(tempfile.gettempdir()),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerUnavailable(
                "Test execution is unavailable because the Docker daemon cannot be reached. "
                "Static repository analysis remains available."
            ) from exc
        if result.returncode:
            raise DockerUnavailable(
                "Test execution is unavailable because the configured immutable runtime image "
                "is not present locally. Build or pull it before running tests."
            )
        return image

    def execution_profile(self) -> dict[str, object]:
        plugin_hash = hashlib.sha256(_PLUGIN_SOURCE.read_bytes()).hexdigest()
        supervisor_hash = hashlib.sha256(_SUPERVISOR_SOURCE.read_bytes()).hexdigest()
        return {
            "plugin_sha256": plugin_hash,
            "supervisor_sha256": supervisor_hash,
            "network": "none",
            "readonly_rootfs": True,
            "repository_mount": "readonly",
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "pids_limit": self.settings.runtime_pids_limit,
            "memory_mb": self.settings.runtime_memory_mb,
            "cpu_count": self.settings.runtime_cpu_count,
            "timeout_seconds": self.settings.runtime_timeout_seconds,
            "output_limit_bytes": self.settings.runtime_output_limit_bytes,
        }

    def collect(
        self,
        snapshot_root: Path,
        snapshot_id: str,
        runtime_environment_id: str,
        run_id: str | None = None,
        cancel_event: threading.Event | None = None,
        source_map: dict[str, object] | None = None,
        materialization_id: str | None = None,
    ) -> TestExecutionResult:
        return self._run(
            snapshot_root,
            snapshot_id,
            runtime_environment_id,
            mode="collect",
            node_ids=(),
            run_id=run_id or stable_id(uuid.uuid4().hex, prefix="run_"),
            cancel_event=cancel_event or threading.Event(),
            source_map=source_map,
            materialization_id=materialization_id,
        )

    def execute(
        self,
        snapshot_root: Path,
        snapshot_id: str,
        runtime_environment_id: str,
        node_ids: Sequence[str],
        run_id: str,
        cancel_event: threading.Event | None = None,
        source_map: dict[str, object] | None = None,
        materialization_id: str | None = None,
    ) -> TestExecutionResult:
        validated = tuple(validate_node_id(node_id) for node_id in node_ids)
        if not validated or len(set(validated)) != len(validated):
            raise ApprovalError("Execution requires unique collected pytest node IDs")
        return self._run(
            snapshot_root,
            snapshot_id,
            runtime_environment_id,
            mode="execute",
            node_ids=validated,
            run_id=run_id,
            cancel_event=cancel_event or threading.Event(),
            source_map=source_map,
            materialization_id=materialization_id,
        )

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            active = self._active.get(run_id)
            if active is None:
                return False
            container_name, _process, cancel_event = active
            cancel_event.set()
        self._kill_container(container_name)
        return True

    def active_container_name(self, run_id: str) -> str | None:
        with self._lock:
            active = self._active.get(run_id)
            return active[0] if active else None

    def cleanup_orphaned_containers(self) -> list[str]:
        """Remove only runtime containers whose recorded owner is demonstrably gone."""
        docker_cli = shutil.which(self.settings.runtime_docker_bin)
        if docker_cli is None:
            return []
        try:
            self._docker_endpoint = self._resolve_local_docker_endpoint(docker_cli)
        except DockerUnavailable as exc:
            self._cleanup_failures["daemon"] = str(exc)
            logger.warning("Docker runtime cleanup could not contact Docker: %s", exc)
            return []
        try:
            listed = subprocess.run(
                [
                    docker_cli,
                    "ps",
                    "-aq",
                    "--filter",
                    "label=com.falkordb-codegraph.runtime=true",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=self._docker_environment(tempfile.gettempdir()),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._cleanup_failures["daemon"] = f"container listing failed: {type(exc).__name__}"
            logger.warning("Docker runtime cleanup could not list containers")
            return []
        if listed.returncode:
            self._cleanup_failures["daemon"] = f"container listing exited {listed.returncode}"
            logger.warning("Docker runtime cleanup could not list containers")
            return []
        removed: list[str] = []
        for container_id in (item.strip() for item in listed.stdout.splitlines()):
            if not container_id:
                continue
            owner_labels = self._runtime_container_owner(docker_cli, container_id)
            if owner_labels is None or self._owner_is_live(owner_labels):
                continue
            if self._kill_container(container_id):
                removed.append(container_id)
        return removed

    def _runtime_container_owner(self, docker_cli: str, container_id: str) -> dict[str, str] | None:
        try:
            inspected = subprocess.run(
                [
                    docker_cli,
                    "inspect",
                    "--format",
                    "{{json .Config.Labels}}",
                    container_id,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=self._docker_environment(tempfile.gettempdir()),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if inspected.returncode:
            return None
        try:
            labels = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(labels, dict) or labels.get("com.falkordb-codegraph.runtime") != "true":
            return None
        return {
            key: value
            for key, value in labels.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _owner_is_live(self, labels: dict[str, str]) -> bool:
        """Return true for a confirmed live owner; unknown ownership is handled safely."""
        if labels.get("com.falkordb-codegraph.owner-instance") == self._instance_id:
            return True
        try:
            owner_pid = int(labels["com.falkordb-codegraph.owner-pid"])
            owner_started_at = labels["com.falkordb-codegraph.owner-started-at"]
        except (KeyError, ValueError):
            return True
        current_start = self._process_start_marker(owner_pid)
        if current_start is None or not owner_started_at:
            return True
        if not current_start:
            return False
        return current_start == owner_started_at

    @staticmethod
    def _process_start_marker(pid: int) -> str | None:
        if pid <= 0:
            return ""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ""
        except PermissionError:
            return None
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode:
            return None
        marker = " ".join(result.stdout.split())
        return marker or None

    def _run(
        self,
        snapshot_root: Path,
        snapshot_id: str,
        runtime_environment_id: str,
        mode: str,
        node_ids: Sequence[str],
        run_id: str,
        cancel_event: threading.Event,
        source_map: dict[str, object] | None,
        materialization_id: str | None,
    ) -> TestExecutionResult:
        with snapshot_lifecycle_lock():
            register_active_snapshot(snapshot_id)
        try:
            verify_runtime_files_root(snapshot_root, snapshot_id, materialization_id)
            image = self.ensure_available()
            return self._run_container(
                image,
                snapshot_root,
                snapshot_id,
                runtime_environment_id,
                mode,
                node_ids,
                run_id,
                cancel_event,
                source_map,
                materialization_id,
            )
        finally:
            with snapshot_lifecycle_lock():
                unregister_active_snapshot(snapshot_id)

    def _run_container(
        self,
        image: str,
        snapshot_root: Path,
        snapshot_id: str,
        runtime_environment_id: str,
        mode: str,
        node_ids: Sequence[str],
        run_id: str,
        cancel_event: threading.Event,
        source_map: dict[str, object] | None,
        materialization_id: str | None,
    ) -> TestExecutionResult:
        root = snapshot_root.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeExecutionError("Snapshot materialization is not a directory")
        started = time.time()
        container_name = f"codegraph-test-{uuid.uuid4().hex}"
        plugin_name = _runtime_plugin_name()
        plugin_target = f"/{plugin_name}.py"
        source_map_fd, source_map_name = tempfile.mkstemp(prefix="codegraph-runtime-map-")
        with os.fdopen(source_map_fd, "w", encoding="utf-8") as source_map_file:
            json.dump(source_map or {}, source_map_file, sort_keys=True)
        os.chmod(source_map_name, 0o444)
        temporary_home = tempfile.mkdtemp(prefix="codegraph-docker-home-")
        container_may_exist = False
        try:
            command = self._container_argv(
                image,
                container_name,
                root,
                plugin_target,
                plugin_name,
                mode,
                node_ids,
                Path(source_map_name),
            )
            env = self._docker_environment(temporary_home)
            with self._lock:
                self._active[run_id] = (container_name, None, cancel_event)
            # Revalidate after command setup and immediately before the Docker CLI can
            # create a container from this mount.
            verify_runtime_files_root(snapshot_root, snapshot_id, materialization_id)
            container_may_exist = True
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            stdout = _OutputCollector(self.settings.runtime_output_limit_bytes)
            stderr = _OutputCollector(self.settings.runtime_output_limit_bytes)
            stdout_thread = threading.Thread(
                target=stdout.drain, args=(process.stdout,), daemon=True
            )
            stderr_thread = threading.Thread(
                target=stderr.drain, args=(process.stderr,), daemon=True
            )
            with self._lock:
                self._active[run_id] = (container_name, process, cancel_event)
            stdout_thread.start()
            stderr_thread.start()
            deadline = time.monotonic() + self.settings.runtime_timeout_seconds
            timed_out = False
            cancelled = False
            while process.poll() is None:
                if cancel_event.wait(0.1):
                    cancelled = True
                    self._kill_container(container_name)
                    self._terminate_docker_cli(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_container(container_name)
                    self._terminate_docker_cli(process)
                    break
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._terminate_docker_cli(process, force=True)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            result_data: dict[str, Any] = {}
            pytest_stdout = ""
            pytest_stderr = ""
            if not (cancelled or timed_out):
                output_text = stdout.value.decode("utf-8", errors="replace")
                result_prefix = "CODEGRAPH_RUNTIME_RESULT:"
                result_lines = [
                    line[len(result_prefix) :]
                    for line in output_text.splitlines()
                    if line.startswith(result_prefix)
                ]
                if not result_lines:
                    raise RuntimeExecutionError(
                        "The isolated pytest supervisor returned no runtime result."
                    )
                try:
                    payload = json.loads(result_lines[-1])
                except json.JSONDecodeError as exc:
                    raise RuntimeExecutionError(
                        "The isolated pytest supervisor returned an invalid runtime result."
                    ) from exc
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("plugin_result"), dict
                ):
                    raise RuntimeExecutionError(
                        "The isolated pytest supervisor returned no valid runtime result."
                    )
                result_data = payload["plugin_result"]
                result_data["exit_code"] = payload.get("exit_code")
                pytest_stdout = str(payload.get("stdout", ""))
                pytest_stderr = str(payload.get("stderr", ""))
            reported_exit_code = result_data.get("exit_code")
            if not isinstance(reported_exit_code, int):
                reported_exit_code = process.returncode or 0
            status = (
                RuntimeRunStatus.CANCELLED
                if cancelled
                else RuntimeRunStatus.TIMED_OUT
                if timed_out
                else RuntimeRunStatus.COMPLETED
                if process.returncode == 0 and reported_exit_code == 0
                else RuntimeRunStatus.FAILED
            )
            return TestExecutionResult(
                run_id=run_id,
                snapshot_id=snapshot_id,
                runtime_environment_id=runtime_environment_id,
                status=status,
                exit_code=(reported_exit_code if not (cancelled or timed_out) else None),
                collected_node_ids=[str(item) for item in result_data.get("node_ids", [])],
                passed=int(result_data.get("passed", 0)),
                failed=int(result_data.get("failed", 0)),
                skipped=int(result_data.get("skipped", 0)),
                environment={
                    str(key): str(value)
                    for key, value in result_data.get("environment", {}).items()
                }
                if isinstance(result_data.get("environment", {}), dict)
                else {},
                runtime_entries=[
                    item
                    for item in result_data.get("runtime_entries", [])
                    if isinstance(item, dict)
                ],
                runtime_calls=[
                    item for item in result_data.get("runtime_calls", []) if isinstance(item, dict)
                ],
                runtime_diagnostics=[
                    item
                    for item in result_data.get("runtime_diagnostics", [])
                    if isinstance(item, dict)
                ],
                stdout=pytest_stdout,
                stderr="".join(
                    value
                    for value in (
                        pytest_stderr,
                        stderr.value.decode("utf-8", errors="replace"),
                    )
                    if value
                ),
                started_at=started,
                ended_at=time.time(),
            )
        except OSError as exc:
            raise DockerUnavailable(
                "Test execution is unavailable because Docker could not start the container."
            ) from exc
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            cleanup_succeeded = (
                self._kill_container(container_name) if container_may_exist else True
            )
            shutil.rmtree(temporary_home, ignore_errors=True)
            Path(source_map_name).unlink(missing_ok=True)
            if not cleanup_succeeded:
                raise RuntimeExecutionError(
                    "Docker did not confirm runtime container cleanup; retry is pending."
                )

    def _container_argv(
        self,
        image: str,
        container_name: str,
        snapshot_root: Path,
        plugin_target: str,
        plugin_name: str,
        mode: str,
        node_ids: Sequence[str],
        source_map_path: Path,
    ) -> list[str]:
        command = [
            shutil.which(self.settings.runtime_docker_bin) or self.settings.runtime_docker_bin,
            "run",
            "--name",
            container_name,
            "--label",
            "com.falkordb-codegraph.runtime=true",
            "--label",
            f"com.falkordb-codegraph.owner-instance={self._instance_id}",
            "--label",
            f"com.falkordb-codegraph.owner-pid={self._owner_pid}",
            "--label",
            f"com.falkordb-codegraph.owner-started-at={self._owner_started_at or ''}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self.settings.runtime_pids_limit),
            "--memory",
            f"{self.settings.runtime_memory_mb}m",
            "--memory-swap",
            f"{self.settings.runtime_memory_mb}m",
            "--cpus",
            str(self.settings.runtime_cpu_count),
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--mount",
            f"type=bind,src={snapshot_root},dst=/workspace,readonly",
            "--mount",
            f"type=bind,src={_PLUGIN_SOURCE.resolve(strict=True)},dst={plugin_target},readonly",
            "--mount",
            f"type=bind,src={source_map_path},dst=/source-map.json,readonly",
            "--mount",
            f"type=bind,src={_SUPERVISOR_SOURCE.resolve(strict=True)},dst=/runtime-supervisor.py,readonly",
        ]
        for key, value in (
            ("HOME", "/tmp"),
            ("TMPDIR", "/tmp"),
            ("PATH", "/usr/local/bin:/usr/bin:/bin"),
            ("PYTHONPATH", "/"),
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("PYTHONUNBUFFERED", "1"),
            ("CODEGRAPH_SOURCE_MAP_PATH", "/source-map.json"),
            ("CODEGRAPH_OUTPUT_LIMIT_BYTES", str(self.settings.runtime_output_limit_bytes)),
        ):
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--entrypoint", "python", image, "/runtime-supervisor.py", "--"])
        if mode == "collect":
            command.extend(deterministic_pytest_command((), plugin_name)[1:-1])
            command.extend(["--collect-only", "/workspace"])
        elif mode == "execute":
            command.extend(deterministic_pytest_command(node_ids, plugin_name)[1:])
        else:
            raise ValueError("Unsupported runtime command type")
        return command

    def _docker_environment(self, home: str) -> dict[str, str]:
        docker_cli = shutil.which(self.settings.runtime_docker_bin)
        docker_path = str(Path(docker_cli).parent) if docker_cli else ""
        config_root = Path(home) / ".docker-isolated"
        config_root.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.pathsep.join(part for part in (docker_path, os.defpath) if part),
            "HOME": home,
            "DOCKER_CONFIG": str(config_root),
            **({"DOCKER_HOST": self._docker_endpoint} if self._docker_endpoint else {}),
        }

    def _resolve_local_docker_endpoint(self, docker_cli: str) -> str:
        context_env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home())}
        try:
            context = subprocess.run(
                [docker_cli, "context", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=context_env,
            )
            name = context.stdout.strip()
            if context.returncode or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise DockerUnavailable("Test execution is unavailable: Docker context is invalid.")
            endpoint_result = subprocess.run(
                [
                    docker_cli,
                    "context",
                    "inspect",
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                    name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=context_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerUnavailable(
                "Test execution is unavailable: Docker context is unavailable."
            ) from exc
        endpoint = endpoint_result.stdout.strip()
        if endpoint_result.returncode or not endpoint.startswith("unix://"):
            raise DockerUnavailable(
                "Test execution requires a local Docker Unix socket; "
                "remote Docker endpoints are disabled."
            )
        return endpoint

    def _kill_container(self, name: str) -> bool:
        docker_cli = (
            shutil.which(self.settings.runtime_docker_bin) or self.settings.runtime_docker_bin
        )
        last_error = "Docker did not confirm container removal"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [docker_cli, "rm", "-f", name],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=3,
                    check=False,
                    env=self._docker_environment(tempfile.gettempdir()),
                )
                if result.returncode == 0:
                    self._cleanup_failures.pop(name, None)
                    return True
                detail = result.stderr.decode("utf-8", "replace").strip()
                if "No such container" in detail:
                    self._cleanup_failures.pop(name, None)
                    return True
                if "removal of container" in detail and "already in progress" in detail:
                    removal_deadline = time.monotonic() + 5
                    while time.monotonic() < removal_deadline:
                        try:
                            inspected = subprocess.run(
                                [docker_cli, "container", "inspect", name],
                                stdin=subprocess.DEVNULL,
                                capture_output=True,
                                timeout=3,
                                check=False,
                                env=self._docker_environment(tempfile.gettempdir()),
                            )
                        except (OSError, subprocess.TimeoutExpired):
                            inspected = None
                        if inspected is not None and inspected.returncode != 0:
                            inspect_detail = inspected.stderr.decode("utf-8", "replace")
                            if (
                                "No such container" in inspect_detail
                                or "No such object" in inspect_detail
                            ):
                                self._cleanup_failures.pop(name, None)
                                return True
                        time.sleep(0.1)
                    last_error = "Docker container removal did not finish within 5 seconds"
                    break
                last_error = detail[:200] or f"docker rm exited {result.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = f"{type(exc).__name__}: Docker cleanup failed or timed out"
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
        self._cleanup_failures[name] = last_error
        logger.error("Runtime container cleanup failed for %s: %s", name, last_error)
        return False

    def _terminate_docker_cli(self, process: subprocess.Popen[bytes], force: bool = False) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


@dataclass(frozen=True)
class _CollectedRuntime:
    materialization_id: str
    snapshot_root: Path
    environment: RuntimeEnvironment
    source_map: dict[str, object]
    source_map_hash: str
    invocation_ids: dict[str, str]
    analysis: SnapshotAnalysis | None


class RuntimeActionService:
    """Coordinates collection, exact user approvals, execution, and cancellation."""

    def __init__(
        self,
        executor: DockerTestExecutor,
        persist_result: Callable[
            [
                RuntimeEnvironment,
                TestExecutionResult,
                list[TestDefinition],
                list[TestInvocation],
                SnapshotAnalysis | None,
            ],
            None,
        ]
        | None = None,
    ) -> None:
        self.executor = executor
        self.persist_result = persist_result
        self.approvals = ApprovalRegistry()
        self._lock = threading.RLock()
        self._collections: dict[tuple[str, str], _CollectedRuntime] = {}
        self._results: dict[str, TestExecutionResult] = {}

    def collect(
        self,
        snapshot_id: str,
        materialization_id: str,
        snapshot_root: Path,
        image_digest: str,
        analysis: SnapshotAnalysis | None = None,
    ) -> RuntimeCollection:
        environment_id = stable_id(snapshot_id, materialization_id, image_digest, prefix="runtime_")
        base_source_map = _runtime_source_map(analysis)
        result = self.executor.collect(
            snapshot_root,
            snapshot_id,
            environment_id,
            source_map=base_source_map,
            materialization_id=materialization_id,
        )
        if result.status != RuntimeRunStatus.COMPLETED:
            raise RuntimeExecutionError(
                f"Isolated pytest collection did not complete successfully ({result.status})"
            )
        environment = RuntimeEnvironment(
            runtime_environment_id=environment_id,
            snapshot_id=snapshot_id,
            python_version=result.environment.get("python_version", "unknown"),
            platform=result.environment.get("platform", "unknown"),
            python_executable_hash=result.environment.get("python_executable_hash", "unknown"),
            sys_path_hash=result.environment.get("sys_path_hash", "unknown"),
            installed_distributions_hash=result.environment.get(
                "installed_distributions_hash", "unknown"
            ),
            source_manifest_hash=materialization_id,
            image_digest=image_digest,
            runtime_materialization_id=materialization_id,
        )
        definitions, invocations = identify_pytest_items(snapshot_id, result.collected_node_ids)
        invocation_ids = {item.node_id: item.test_invocation_id for item in invocations}
        source_map = _runtime_source_map(analysis, invocation_ids)
        source_map_hash = hashlib.sha256(
            json.dumps(source_map, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.approvals.register_collection(snapshot_id, environment_id, result.collected_node_ids)
        with self._lock:
            self._collections[(snapshot_id, environment_id)] = _CollectedRuntime(
                materialization_id=materialization_id,
                snapshot_root=snapshot_root,
                environment=environment,
                source_map=source_map,
                source_map_hash=source_map_hash,
                invocation_ids=invocation_ids,
                analysis=analysis,
            )
        if self.persist_result:
            # Collection imports repository modules in the isolated container. Keep any
            # attributable calls as session-level observations, never as test coverage.
            self.persist_result(environment, result, [], [], analysis)
        return RuntimeCollection(
            snapshot_id=snapshot_id,
            runtime_environment=environment,
            test_definitions=definitions,
            test_invocations=invocations,
            collection_result=result,
        )

    def prepare(
        self,
        snapshot_id: str,
        runtime_environment_id: str,
        test_ids: Sequence[str],
    ) -> ActionApproval:
        with self._lock:
            collection = self._collections.get((snapshot_id, runtime_environment_id))
        if collection is None:
            raise ApprovalError("Collect tests for this snapshot and environment first")
        if not collection.environment.image_digest:
            raise ApprovalError("Runtime environment has no immutable image identity")
        return self.approvals.prepare(
            snapshot_id,
            runtime_environment_id,
            test_ids,
            collection.environment.image_digest,
            self.executor.execution_profile() | {"source_map_hash": collection.source_map_hash},
        )

    def approve(self, approval_id: str) -> ActionApproval:
        return self.approvals.approve(approval_id)

    def revoke(self, approval_id: str) -> ActionApproval:
        return self.approvals.revoke(approval_id)

    def execute(self, approval_id: str) -> TestExecutionResult:
        approval = self.approvals.get(approval_id)
        with self._lock:
            collection = self._collections.get(
                (approval.snapshot_id, approval.runtime_environment_id)
            )
        if collection is None or not collection.environment.image_digest:
            raise ApprovalError("Approved runtime environment is no longer available")
        consumed, node_ids = self.approvals.consume(
            approval_id,
            approval.snapshot_id,
            approval.runtime_environment_id,
            approval.test_ids,
            collection.environment.image_digest,
            self.executor.execution_profile() | {"source_map_hash": collection.source_map_hash},
        )
        if not consumed.run_id:
            raise ApprovalError("Approval has no execution run identity")
        source_map = _runtime_source_map(
            collection.analysis,
            {
                node_id: collection.invocation_ids[node_id]
                for node_id in node_ids
                if node_id in collection.invocation_ids
            },
        )
        result = self.executor.execute(
            collection.snapshot_root,
            consumed.snapshot_id,
            consumed.runtime_environment_id,
            node_ids,
            consumed.run_id,
            source_map=source_map,
            materialization_id=collection.materialization_id,
        )
        if collection.analysis is not None:
            validation, diagnostics = validate_runtime_calls(collection.analysis, result)
            runtime_diagnostics = [
                *result.runtime_diagnostics,
                *(item.model_dump(mode="json") for item in diagnostics),
            ]
            result = result.model_copy(
                update={
                    "runtime_validation": validation,
                    "runtime_diagnostics": runtime_diagnostics,
                }
            )
        definitions, invocations = identify_pytest_items(consumed.snapshot_id, node_ids)
        with self._lock:
            self._results[result.run_id] = result
        if self.persist_result:
            self.persist_result(
                collection.environment,
                result,
                definitions,
                invocations,
                collection.analysis,
            )
        return result

    def cancel(self, run_id: str) -> bool:
        return self.executor.cancel(run_id)

    def get_result(self, run_id: str) -> TestExecutionResult | None:
        with self._lock:
            result = self._results.get(run_id)
        return result.model_copy(deep=True) if result else None

    def approval_state(self, approval_id: str) -> ActionApproval:
        return self.approvals.get_state(approval_id)

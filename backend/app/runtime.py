from __future__ import annotations

import contextvars
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .ids import stable_id
from .models import (
    RuntimeContext,
    RuntimeDiagnostic,
    RuntimeEnvironment,
    RuntimeFunctionEntry,
    SnapshotAnalysis,
    TestDefinition,
    TestInvocation,
)

_CURRENT_INVOCATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "codegraph_test_invocation", default=None
)


def _split_pytest_node_id(snapshot_id: str, node_id: str) -> tuple[TestDefinition, TestInvocation]:
    file_path, separator, test_path = node_id.partition("::")
    if not separator or not file_path or not test_path:
        raise ValueError("Pytest node ID must include a repository-relative file and test item")
    normalized_path = file_path.replace("\\", "/")
    path = PurePosixPath(normalized_path)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
        raise ValueError("Pytest node ID must use a safe repository-relative Python file path")

    segments = test_path.split("::")
    if any(not segment for segment in segments):
        raise ValueError("Pytest node ID contains an empty test path segment")
    pytest_name = segments[-1]
    parameter_id: str | None = None
    parameter_start = pytest_name.rfind("[")
    if parameter_start >= 0 and pytest_name.endswith("]"):
        parameter_id = pytest_name[parameter_start + 1 : -1]
        pytest_name = pytest_name[:parameter_start]
    if not pytest_name:
        raise ValueError("Pytest node ID must include a test item name")
    qualified_name = ".".join([*segments[:-1], pytest_name])
    normalized_node_id = f"{normalized_path}::{test_path}"
    definition = TestDefinition(
        test_definition_id=stable_id(
            snapshot_id, normalized_path, qualified_name, prefix="testdef_"
        ),
        snapshot_id=snapshot_id,
        file_path=normalized_path,
        qualified_name=qualified_name,
        pytest_name=pytest_name,
    )
    invocation = TestInvocation(
        test_invocation_id=stable_id(snapshot_id, normalized_node_id, prefix="testinv_"),
        snapshot_id=snapshot_id,
        test_definition_id=definition.test_definition_id,
        node_id=normalized_node_id,
        parameter_id=parameter_id,
    )
    return definition, invocation


def identify_pytest_items(
    snapshot_id: str, node_ids: Iterable[str]
) -> tuple[list[TestDefinition], list[TestInvocation]]:
    """Create stable definitions and per-parameter invocations from collected pytest node IDs.

    The input must come from pytest's item collection output inside the approved runtime
    boundary. Collection-only/session pseudo-items and fixtures are never synthesized into
    TestInvocation records here.
    """
    definitions: dict[str, TestDefinition] = {}
    invocations: dict[str, TestInvocation] = {}
    seen_node_ids: set[str] = set()
    for node_id in sorted(node_ids):
        definition, invocation = _split_pytest_node_id(snapshot_id, node_id)
        if invocation.node_id in seen_node_ids:
            raise ValueError(f"Duplicate pytest node ID: {invocation.node_id}")
        seen_node_ids.add(invocation.node_id)
        definitions[definition.test_definition_id] = definition
        invocations[invocation.test_invocation_id] = invocation
    return (
        sorted(definitions.values(), key=lambda item: (item.file_path, item.qualified_name)),
        sorted(invocations.values(), key=lambda item: item.node_id),
    )


def map_test_definition(analysis: SnapshotAnalysis, definition: TestDefinition) -> str | None:
    """Map a collected item to one source function/method in the same file, if unique."""
    suffix = f".{definition.qualified_name}"
    matches = [
        entity
        for entity in analysis.entities
        if entity.file_path == definition.file_path
        and entity.kind in {"Function", "Method"}
        and (
            entity.qualified_name == definition.qualified_name
            or entity.qualified_name.endswith(suffix)
        )
    ]
    return matches[0].entity_id if len(matches) == 1 else None


class FunctionEntryTracer:
    """Capture Python call events that map to indexed source functions and methods."""

    def __init__(
        self,
        analysis: SnapshotAnalysis,
        run_id: str,
        runtime_environment: RuntimeEnvironment,
    ) -> None:
        self.analysis = analysis
        self.run_id = run_id
        self.runtime_environment = runtime_environment
        self.root = Path(analysis.repository.root).resolve()
        self.entries: list[RuntimeFunctionEntry] = []
        self.diagnostics: list[RuntimeDiagnostic] = []
        self._previous_profile: Any = None
        self._previous_thread_profile: Any = None
        self._lock = threading.RLock()
        self._sequence = 0
        self._unmapped: set[tuple[str, int, str]] = set()
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("Function entry tracer is already running")
        self._previous_profile = sys.getprofile()
        self._previous_thread_profile = threading.getprofile()
        sys.setprofile(self._profile(None))
        threading.setprofile(self._profile(None))
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        sys.setprofile(self._previous_profile)
        threading.setprofile(self._previous_thread_profile)
        self._started = False

    @contextmanager
    def test_invocation(self, invocation_id: str) -> Iterator[None]:
        previous_invocation = _CURRENT_INVOCATION.get()
        token = _CURRENT_INVOCATION.set(invocation_id)
        # New threads inherit this fixed invocation context for their function profile.
        threading.setprofile(self._profile(invocation_id))
        try:
            yield
        finally:
            _CURRENT_INVOCATION.reset(token)
            threading.setprofile(self._profile(previous_invocation))

    def _profile(self, invocation_id: str | None):
        def callback(frame, event: str, _arg: object) -> None:
            if event == "call":
                self._record_call(frame, invocation_id)

        return callback

    def _record_call(self, frame, invocation_id: str | None) -> None:
        code = frame.f_code
        if code.co_name == "<module>":
            return
        try:
            source_path = Path(code.co_filename).resolve()
            relative_path = source_path.relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return

        matches = [
            entity
            for entity in self.analysis.entities
            if entity.file_path == relative_path
            and entity.kind in {"Function", "Method"}
            and (
                entity.qualified_name == code.co_qualname
                or entity.qualified_name.endswith(f".{code.co_qualname}")
            )
        ]
        if len(matches) != 1:
            self._record_unmapped(relative_path, code.co_firstlineno, code.co_qualname)
            return

        current_invocation = invocation_id or _CURRENT_INVOCATION.get()
        context = (
            RuntimeContext.TEST_INVOCATION
            if current_invocation is not None
            else RuntimeContext.COLLECTION_OR_SESSION
        )
        with self._lock:
            self._sequence += 1
            self.entries.append(
                RuntimeFunctionEntry(
                    entry_id=stable_id(self.run_id, self._sequence, prefix="entry_"),
                    run_id=self.run_id,
                    snapshot_id=self.analysis.snapshot_id,
                    runtime_environment_id=self.runtime_environment.runtime_environment_id,
                    test_invocation_id=current_invocation,
                    context=context,
                    entity_id=matches[0].entity_id,
                    observed_at=time.time(),
                )
            )

    def _record_unmapped(self, file_path: str, first_line: int, qualified_name: str) -> None:
        signature = (file_path, first_line, qualified_name)
        with self._lock:
            if signature in self._unmapped:
                return
            self._unmapped.add(signature)
            self.diagnostics.append(
                RuntimeDiagnostic(
                    diagnostic_id=stable_id(self.run_id, *signature, prefix="rdiag_"),
                    run_id=self.run_id,
                    snapshot_id=self.analysis.snapshot_id,
                    code="UNMAPPED_RUNTIME_FUNCTION",
                    message=(
                        "Runtime function entry could not be mapped to a source function or method."
                    ),
                    file_path=file_path,
                    qualified_name=qualified_name,
                    first_line=first_line,
                )
            )

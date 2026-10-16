from __future__ import annotations

import platform
import threading

import pytest

from backend.app.models import (
    RepositoryInfo,
    RuntimeContext,
    RuntimeEnvironment,
    SnapshotAnalysis,
    SnapshotStatus,
    SourceEntity,
    SourceSpan,
)
from backend.app.runtime import FunctionEntryTracer, identify_pytest_items, map_test_definition


def test_parametrized_pytest_items_have_distinct_invocations_and_one_definition() -> None:
    definitions, invocations = identify_pytest_items(
        "snap_fixture",
        [
            "tests/unit/test_flow.py::TestFlow::test_case[alpha]",
            "tests/unit/test_flow.py::TestFlow::test_case[beta]",
        ],
    )

    assert len(definitions) == 1
    assert definitions[0].file_path == "tests/unit/test_flow.py"
    assert definitions[0].qualified_name == "TestFlow.test_case"
    assert len(invocations) == 2
    assert len({item.test_invocation_id for item in invocations}) == 2
    assert {item.test_definition_id for item in invocations} == {definitions[0].test_definition_id}
    assert {item.parameter_id for item in invocations} == {"alpha", "beta"}


def test_duplicate_test_module_basenames_remain_distinct() -> None:
    definitions, invocations = identify_pytest_items(
        "snap_fixture",
        [
            "tests/unit/test_flow.py::test_checkout",
            "tests/integration/test_flow.py::test_checkout",
        ],
    )

    assert len(definitions) == 2
    assert len(invocations) == 2
    assert len({item.test_definition_id for item in definitions}) == 2
    assert {item.file_path for item in definitions} == {
        "tests/unit/test_flow.py",
        "tests/integration/test_flow.py",
    }


def test_collection_items_do_not_create_fixture_invocations() -> None:
    definitions, invocations = identify_pytest_items(
        "snap_fixture",
        ["tests/unit/test_flow.py::test_uses_store[postgres]"],
    )

    assert [item.pytest_name for item in definitions] == ["test_uses_store"]
    assert len(invocations) == 1
    assert all("database" not in item.qualified_name for item in definitions)


@pytest.mark.parametrize(
    "node_id",
    [
        "../outside/test_bad.py::test_case",
        "/tmp/test_bad.py::test_case",
        "tests/test_bad.txt::test_case",
        "tests/test_bad.py",
        "tests/test_bad.py::",
    ],
)
def test_invalid_or_escaping_pytest_node_ids_are_rejected(node_id: str) -> None:
    with pytest.raises(ValueError):
        identify_pytest_items("snap_fixture", [node_id])


def test_repeated_collected_node_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate pytest node ID"):
        identify_pytest_items(
            "snap_fixture",
            ["tests/test_flow.py::test_case", "tests/test_flow.py::test_case"],
        )


def test_test_definition_mapping_uses_path_and_qualified_name() -> None:
    definitions, _ = identify_pytest_items(
        "snap_fixture", ["tests/unit/test_flow.py::TestFlow::test_case"]
    )
    analysis = SnapshotAnalysis(
        repository=RepositoryInfo(
            repository_id="repo_fixture",
            root="/workspace/project",
            remote=None,
            head_commit=None,
            is_dirty=False,
        ),
        snapshot_id="snap_fixture",
        status=SnapshotStatus.READY,
        manifest=[],
        entities=[
            SourceEntity(
                entity_id="entity_test_case",
                kind="Method",
                name="test_case",
                qualified_name="tests.unit.test_flow.TestFlow.test_case",
                file_path="tests/unit/test_flow.py",
                span=SourceSpan(
                    start_byte=0,
                    end_byte=10,
                    start_line=1,
                    start_column=0,
                    end_line=1,
                    end_column=10,
                ),
            ),
            SourceEntity(
                entity_id="entity_duplicate",
                kind="Method",
                name="test_case",
                qualified_name="tests.integration.test_flow.TestFlow.test_case",
                file_path="tests/integration/test_flow.py",
                span=SourceSpan(
                    start_byte=0,
                    end_byte=10,
                    start_line=1,
                    start_column=0,
                    end_line=1,
                    end_column=10,
                ),
            ),
        ],
        sites=[],
        provenance=[],
        diagnostics=[],
        counts={},
    )

    assert map_test_definition(analysis, definitions[0]) == "entity_test_case"


def _runtime_analysis(root: str, file_path: str = "pkg/module.py") -> SnapshotAnalysis:
    return SnapshotAnalysis(
        repository=RepositoryInfo(
            repository_id="repo_fixture",
            root=root,
            remote=None,
            head_commit=None,
            is_dirty=False,
        ),
        snapshot_id="snap_fixture",
        status=SnapshotStatus.READY,
        manifest=[],
        entities=[
            SourceEntity(
                entity_id="entity_target",
                kind="Function",
                name="target",
                qualified_name="pkg.module.target",
                file_path=file_path,
                span=SourceSpan(
                    start_byte=0,
                    end_byte=20,
                    start_line=1,
                    start_column=0,
                    end_line=2,
                    end_column=0,
                ),
            )
        ],
        sites=[],
        provenance=[],
        diagnostics=[],
        counts={},
    )


def _runtime_environment() -> RuntimeEnvironment:
    return RuntimeEnvironment(
        runtime_environment_id="runtime_fixture",
        snapshot_id="snap_fixture",
        python_version=platform.python_version(),
        platform="test-platform",
        python_executable_hash="python-hash",
        sys_path_hash="path-hash",
        installed_distributions_hash="distributions-hash",
        source_manifest_hash="manifest-hash",
    )


def test_function_entry_tracer_ignores_function_definition_until_call(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source_path = root / "pkg" / "module.py"
    source_path.parent.mkdir()
    namespace: dict[str, object] = {}
    code = compile("def target():\n    return 7\n", str(source_path), "exec")
    tracer = FunctionEntryTracer(
        _runtime_analysis(str(root)), "run_fixture", _runtime_environment()
    )

    tracer.start()
    try:
        exec(code, namespace)
        assert tracer.entries == []
        with tracer.test_invocation("invocation_fixture"):
            namespace["target"]()  # type: ignore[operator]
    finally:
        tracer.stop()

    assert len(tracer.entries) == 1
    assert tracer.entries[0].entity_id == "entity_target"
    assert tracer.entries[0].context == RuntimeContext.TEST_INVOCATION
    assert tracer.entries[0].test_invocation_id == "invocation_fixture"


def test_function_entry_tracer_captures_threaded_calls_in_test_context(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source_path = root / "pkg" / "module.py"
    source_path.parent.mkdir()
    namespace: dict[str, object] = {}
    exec(compile("def target():\n    return 7\n", str(source_path), "exec"), namespace)
    tracer = FunctionEntryTracer(
        _runtime_analysis(str(root)), "run_fixture", _runtime_environment()
    )

    tracer.start()
    try:
        with tracer.test_invocation("invocation_fixture"):
            worker = threading.Thread(target=namespace["target"])  # type: ignore[arg-type]
            worker.start()
            worker.join(timeout=2)
    finally:
        tracer.stop()

    assert not worker.is_alive()
    assert len(tracer.entries) == 1
    assert tracer.entries[0].test_invocation_id == "invocation_fixture"


def test_function_entry_tracer_reports_unmapped_runtime_code_once(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source_path = root / "pkg" / "module.py"
    source_path.parent.mkdir()
    namespace: dict[str, object] = {}
    exec(compile("def unindexed():\n    return 1\n", str(source_path), "exec"), namespace)
    tracer = FunctionEntryTracer(
        _runtime_analysis(str(root)), "run_fixture", _runtime_environment()
    )

    tracer.start()
    try:
        namespace["unindexed"]()  # type: ignore[operator]
        namespace["unindexed"]()  # type: ignore[operator]
    finally:
        tracer.stop()

    assert tracer.entries == []
    assert len(tracer.diagnostics) == 1
    assert tracer.diagnostics[0].code == "UNMAPPED_RUNTIME_FUNCTION"

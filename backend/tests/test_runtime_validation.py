from __future__ import annotations

from backend.app.models import (
    DispatchState,
    RepositoryInfo,
    RuntimeRunStatus,
    RuntimeSiteValidationState,
    SiteDispatch,
    SiteKind,
    SnapshotAnalysis,
    SnapshotStatus,
    SourceEntity,
    SourceSite,
    SourceSpan,
)
from backend.app.models import (
    TestExecutionResult as RuntimeExecutionResult,
)
from backend.app.runtime_validation import validate_runtime_calls


def _analysis(*, candidates: list[str], state: DispatchState = DispatchState.SINGLE_CANDIDATE):
    return SnapshotAnalysis(
        repository=RepositoryInfo(
            repository_id="repo_fixture",
            root="/workspace/project",
            remote=None,
            head_commit=None,
            is_dirty=False,
        ),
        snapshot_id="snap_runtime_validation",
        status=SnapshotStatus.READY,
        manifest=[],
        entities=[
            SourceEntity(
                entity_id=entity_id,
                kind="Function",
                name=entity_id,
                qualified_name=f"pkg.{entity_id}",
                file_path="pkg/module.py",
                span=SourceSpan(
                    start_byte=index * 10,
                    end_byte=index * 10 + 9,
                    start_line=index + 1,
                    start_column=0,
                    end_line=index + 1,
                    end_column=9,
                ),
            )
            for index, entity_id in enumerate(("entity_source", "entity_target", "entity_other"))
        ],
        sites=[
            SourceSite(
                site_id="site_call",
                kind=SiteKind.CALL_CALLEE,
                expression="target()",
                file_path="pkg/module.py",
                span=SourceSpan(
                    start_byte=100,
                    end_byte=108,
                    start_line=10,
                    start_column=4,
                    end_line=10,
                    end_column=12,
                ),
                context_id="entity_source",
                syntax_node_type="identifier",
            )
        ],
        dispatch=[
            SiteDispatch(
                site_id="site_call",
                state=state,
                candidate_entity_ids=candidates,
            )
        ],
        provenance=[],
        diagnostics=[],
        counts={},
    )


def _result(calls: list[dict[str, object]]) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        run_id="run_validation",
        snapshot_id="snap_runtime_validation",
        runtime_environment_id="runtime_validation",
        status=RuntimeRunStatus.COMPLETED,
        started_at=1.0,
        ended_at=2.0,
        runtime_calls=calls,
    )


def _call(target_id: str = "entity_target", *, site_id: str | None = "site_call"):
    return {
        "source_entity_id": "entity_source",
        "target_entity_id": target_id,
        "site_id": site_id,
        "test_invocation_id": "invocation_one",
    }


def test_expected_static_candidate_observed_and_run_aggregate_preserves_count() -> None:
    result = _result([_call(), _call()])
    report, diagnostics = validate_runtime_calls(_analysis(candidates=["entity_target"]), result)

    assert report.observed_call_count == 2
    assert report.mapped_call_count == 2
    assert report.mismatch_count == 0
    assert len(result.runtime_calls) == 2
    assert len(report.aggregates) == 1
    assert report.aggregates[0].observation_count == 2
    assert report.sites[0].state == RuntimeSiteValidationState.MATCH
    assert diagnostics == []


def test_runtime_target_outside_static_candidates_creates_mismatch_diagnostic() -> None:
    report, diagnostics = validate_runtime_calls(
        _analysis(candidates=["entity_target"]), _result([_call("entity_other")])
    )

    assert report.mismatch_count == 1
    assert report.sites[0].state == RuntimeSiteValidationState.MISMATCH
    assert diagnostics[0].code == "RUNTIME_MISMATCH"
    assert diagnostics[0].site_id == "site_call"
    assert "pkg.entity_other" in diagnostics[0].message


def test_unobserved_static_candidate_is_not_a_mismatch() -> None:
    report, diagnostics = validate_runtime_calls(
        _analysis(candidates=["entity_target"]), _result([])
    )

    assert report.mismatch_count == 0
    assert report.sites[0].state == RuntimeSiteValidationState.UNOBSERVED
    assert report.sites[0].unobserved_candidate_entity_ids == ["entity_target"]
    assert diagnostics == []


def test_unresolved_static_site_accepts_mapped_runtime_evidence_without_mismatch() -> None:
    report, diagnostics = validate_runtime_calls(
        _analysis(candidates=[], state=DispatchState.UNKNOWN), _result([_call()])
    )

    assert report.mapped_call_count == 1
    assert report.mismatch_count == 0
    assert report.sites[0].state == RuntimeSiteValidationState.UNRESOLVED
    assert diagnostics == []


def test_empty_static_candidate_set_stays_unresolved_even_if_marked_single_candidate() -> None:
    report, diagnostics = validate_runtime_calls(
        _analysis(candidates=[], state=DispatchState.SINGLE_CANDIDATE), _result([_call()])
    )

    assert report.mapped_call_count == 1
    assert report.mismatch_count == 0
    assert report.sites[0].state == RuntimeSiteValidationState.UNRESOLVED
    assert diagnostics == []


def test_unmapped_runtime_callsite_is_diagnostic_not_a_static_mismatch() -> None:
    report, diagnostics = validate_runtime_calls(
        _analysis(candidates=["entity_target"]), _result([_call(site_id=None)])
    )

    assert report.unmapped_call_count == 1
    assert report.mismatch_count == 0
    assert diagnostics[0].code == "UNMAPPED_RUNTIME_CALLSITE"
    assert diagnostics[0].site_id is None

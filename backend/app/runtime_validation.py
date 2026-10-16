from __future__ import annotations

from collections import Counter, defaultdict

from .ids import stable_id
from .models import (
    DispatchState,
    RuntimeCallAggregate,
    RuntimeDiagnostic,
    RuntimeSiteValidation,
    RuntimeSiteValidationState,
    RuntimeValidationReport,
    SiteKind,
    SnapshotAnalysis,
    TestExecutionResult,
)


def validate_runtime_calls(
    analysis: SnapshotAnalysis, result: TestExecutionResult
) -> tuple[RuntimeValidationReport, list[RuntimeDiagnostic]]:
    """Compare observed calls to static dispatch candidates without changing static facts."""
    if result.snapshot_id != analysis.snapshot_id:
        raise ValueError("Runtime validation crossed snapshot scope")
    sites = {site.site_id: site for site in analysis.sites if site.kind == SiteKind.CALL_CALLEE}
    dispatch = {state.site_id: state for state in analysis.dispatch}
    entities = {entity.entity_id: entity for entity in analysis.entities}
    observation_counts: Counter[str] = Counter()
    targets_by_site: dict[str, set[str]] = defaultdict(set)
    aggregates: Counter[tuple[str, str, str]] = Counter()
    invocation_ids: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    unmapped = 0
    diagnostics: list[RuntimeDiagnostic] = []

    for index, call in enumerate(result.runtime_calls):
        site_id_value = call.get("site_id")
        site_id = str(site_id_value) if site_id_value else None
        source_id = str(call.get("source_entity_id", ""))
        target_id = str(call.get("target_entity_id", ""))
        site = sites.get(site_id) if site_id else None
        if (
            site is None
            or site.context_id != source_id
            or source_id not in entities
            or target_id not in entities
        ):
            unmapped += 1
            diagnostics.append(
                RuntimeDiagnostic(
                    diagnostic_id=stable_id(
                        result.run_id, "unmapped-callsite", index, call, prefix="rdiag_"
                    ),
                    run_id=result.run_id,
                    snapshot_id=result.snapshot_id,
                    code="UNMAPPED_RUNTIME_CALLSITE",
                    message=(
                        "Runtime call could not be joined to one static call site and indexed "
                        "source/target pair."
                    ),
                    file_path=str(call.get("file_path", "")),
                    qualified_name=str(call.get("qualified_name", "")),
                    first_line=_as_int(call.get("first_line"), 1),
                    site_id=None,
                )
            )
            continue
        assert site_id is not None
        observation_counts[site_id] += 1
        targets_by_site[site_id].add(target_id)
        key = (site_id, source_id, target_id)
        aggregates[key] += 1
        invocation_id = call.get("test_invocation_id")
        if invocation_id:
            invocation_ids[key].add(str(invocation_id))

    site_reports: list[RuntimeSiteValidation] = []
    mismatch_keys: set[tuple[str, str]] = set()
    for site_id in sorted(sites):
        static_state = dispatch.get(site_id)
        candidates = sorted(static_state.candidate_entity_ids) if static_state else []
        observed = sorted(targets_by_site.get(site_id, set()))
        unmatched_targets: list[str] = []
        has_static_candidates = bool(candidates) and static_state is not None
        if (
            static_state is not None
            and has_static_candidates
            and static_state.state != DispatchState.UNKNOWN
        ):
            unmatched_targets = [target for target in observed if target not in candidates]
        mismatch_keys.update((site_id, target) for target in unmatched_targets)
        if observation_counts[site_id] == 0:
            state = RuntimeSiteValidationState.UNOBSERVED
        elif unmatched_targets:
            state = RuntimeSiteValidationState.MISMATCH
        elif not has_static_candidates:
            state = RuntimeSiteValidationState.UNRESOLVED
        else:
            state = RuntimeSiteValidationState.MATCH
        site_reports.append(
            RuntimeSiteValidation(
                site_id=site_id,
                state=state,
                static_candidate_entity_ids=candidates,
                observed_target_entity_ids=observed,
                unobserved_candidate_entity_ids=sorted(set(candidates) - set(observed)),
                observation_count=observation_counts[site_id],
            )
        )

    for site_id, target_id in sorted(mismatch_keys):
        site = sites[site_id]
        candidates = sorted(dispatch[site_id].candidate_entity_ids)
        target = entities[target_id]
        diagnostics.append(
            RuntimeDiagnostic(
                diagnostic_id=stable_id(
                    result.run_id, "runtime-mismatch", site_id, target_id, prefix="rdiag_"
                ),
                run_id=result.run_id,
                snapshot_id=result.snapshot_id,
                code="RUNTIME_MISMATCH",
                message=(
                    f"Runtime observed {target.qualified_name}; static dispatch candidates were "
                    f"{', '.join(entities[item].qualified_name for item in candidates)}."
                ),
                file_path=site.file_path,
                qualified_name=site.expression,
                first_line=site.span.start_line,
                site_id=site_id,
            )
        )

    aggregate_rows = [
        RuntimeCallAggregate(
            site_id=site_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            observation_count=count,
            test_invocation_ids=sorted(invocation_ids[(site_id, source_id, target_id)]),
        )
        for (site_id, source_id, target_id), count in sorted(aggregates.items())
    ]
    report = RuntimeValidationReport(
        run_id=result.run_id,
        snapshot_id=result.snapshot_id,
        observed_call_count=len(result.runtime_calls),
        mapped_call_count=sum(observation_counts.values()),
        unmapped_call_count=unmapped,
        mismatch_count=len(mismatch_keys),
        aggregates=aggregate_rows,
        sites=site_reports,
    )
    return report, diagnostics


def _as_int(value: object, fallback: int) -> int:
    return value if isinstance(value, int) else fallback

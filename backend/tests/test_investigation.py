from __future__ import annotations

import asyncio

from backend.app.claims import ClaimStatus, ClaimType
from backend.app.fusion import EvidenceType
from backend.app.investigation import build_candidate_draft, build_investigation_response
from backend.app.orchestrator import QueryOrchestrator
from backend.tests.test_graph_tools import SNAPSHOT
from backend.tests.test_orchestrator import _tools


def test_investigation_builds_only_validated_exact_call_cards() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute("Who are the callers of `callee`?", snapshot_id=SNAPSHOT)
    )

    result = build_investigation_response(query, tools)

    assert result.snapshot_id == SNAPSHOT
    assert result.answer.claims
    claim = result.answer.claims[0]
    assert claim.claim_type == ClaimType.DIRECT_CALL
    assert claim.status == ClaimStatus.SUPPORTED
    assert claim.evidence[0].item.type == EvidenceType.STATIC_BINDING
    assert claim.evidence[0].item.snapshot_id == SNAPSHOT
    assert claim.rendered_text == "caller has an exact static call binding to callee."
    assert "text" not in claim.rendered_text


def test_investigation_evidence_path_is_ordered_and_provenance_backed() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "Find paths from `caller` to `callee`", snapshot_id=SNAPSHOT
        )
    )

    result = build_investigation_response(query, tools)

    assert len(result.evidence_paths) == 2
    assert len({path.claim_id for path in result.evidence_paths}) == 2
    assert len({path.steps[0].edge_ids for path in result.evidence_paths}) == 2
    path = result.evidence_paths[0]
    assert path.snapshot_id == SNAPSHOT
    assert [node.canonical_id for node in path.nodes] == ["caller", "callee"]
    assert path.hop_count == 1
    assert len(path.steps) == 1
    step = path.steps[0]
    assert step.relation
    assert step.modality == "EXACT"
    assert step.edge_ids
    assert step.evidence_ids
    evidence_by_id = {item.item.evidence_id: item.item for item in result.query.evidence_pool.items}
    assert all(evidence_by_id[item].provenance_pointer for item in step.evidence_ids)
    claim = next(item for item in result.answer.claims if item.claim_id == path.claim_id)
    assert claim.status == ClaimStatus.SUPPORTED
    assert set(claim.evidence_ids) == set(step.evidence_ids)
    assert result.unresolved_barriers
    assert result.unresolved_barriers[0].resolution == "UNRESOLVED"


def test_conservative_dispatch_path_remains_qualified_in_path_projection() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "Could `caller` possibly reach `derived-run` through dispatch?",
            snapshot_id=SNAPSHOT,
        )
    )

    assert query.plan.traversal_mode == "CONSERVATIVE_DISPATCH"
    execution = next(item for item in query.tool_executions if item.result.tool == "find_paths")
    assert execution.request.mode == "CONSERVATIVE_DISPATCH"
    result = build_investigation_response(query, tools)

    assert result.evidence_paths
    assert all(
        step.modality == "MAY_DISPATCH" for path in result.evidence_paths for step in path.steps
    )
    assert all(
        claim.status == ClaimStatus.QUALIFIED and claim.modality.value == "MAY"
        for claim in result.answer.claims
        if claim.claim_type == ClaimType.STRUCTURAL_PATH
    )


def test_retrieved_passages_remain_passage_claims_and_never_paths() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "Which tests observed callers of `callee` at `caller`?", snapshot_id=SNAPSHOT
        )
    )

    draft = build_candidate_draft(query)
    result = build_investigation_response(query, tools)
    claims_by_id = {claim.claim_id: claim for claim in result.answer.claims}
    passage_claims = [
        claim for claim in draft.claims if claim.claim_type == ClaimType.PASSAGE_STATEMENT
    ]

    assert passage_claims
    assert all(claim.claim_id in claims_by_id for claim in passage_claims)
    assert all(claim.claim_type == ClaimType.PASSAGE_STATEMENT for claim in passage_claims)
    assert all(
        claim.status == ClaimStatus.QUALIFIED
        for claim in (claims_by_id[item.claim_id] for item in passage_claims)
    )
    assert result.evidence_paths == ()
    assert all(
        ref.item.type in {EvidenceType.LEXICAL_PASSAGE, EvidenceType.SEMANTIC_PASSAGE}
        for claim in (claims_by_id[item.claim_id] for item in passage_claims)
        for ref in claim.evidence
    )


def test_investigation_is_deterministic_for_the_same_snapshot_query() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute("Who are the callers of `callee`?", snapshot_id=SNAPSHOT)
    )

    first = build_investigation_response(query, tools)
    second = build_investigation_response(query, tools)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_selected_call_site_followup_uses_the_selected_snapshot_site() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "What target does this call site reach?",
            snapshot_id=SNAPSHOT,
            selected_site_id="call-known",
        )
    )

    result = build_investigation_response(query, tools)

    assert result.query.plan.selected_site_id == "call-known"
    assert any(
        claim.claim_type == ClaimType.DIRECT_CALL
        and claim.subject_id == "caller"
        and claim.object_id == "callee"
        and claim.status == ClaimStatus.SUPPORTED
        for claim in result.answer.claims
    )


def test_selected_unresolved_site_cannot_borrow_an_exact_sibling_binding() -> None:
    tools = _tools()
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "What target does this call site reach?",
            snapshot_id=SNAPSHOT,
            selected_site_id="call-unknown",
        )
    )

    callee_executions = [
        execution for execution in query.tool_executions if execution.result.tool == "find_callees"
    ]

    assert query.plan.selected_site_id == "call-unknown"
    assert [execution.request.site_id for execution in callee_executions] == ["call-unknown"]
    assert callee_executions[0].result.results == ()
    answer = build_investigation_response(query, tools).answer
    assert not any(claim.claim_type == ClaimType.DIRECT_CALL for claim in answer.claims)


def test_context_wide_callee_query_still_includes_sibling_sites() -> None:
    query = asyncio.run(
        QueryOrchestrator(_tools()).execute("What does `caller` call?", snapshot_id=SNAPSHOT)
    )
    callee_executions = [
        execution for execution in query.tool_executions if execution.result.tool == "find_callees"
    ]
    assert query.plan.selected_site_id is None
    assert {execution.request.site_id for execution in callee_executions} >= {
        "call-known",
        "call-known-parallel",
        "call-unknown",
    }

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.main import QueryRequest, query_plan
from backend.app.orchestrator import QueryOrchestrator, RouteValidator
from backend.app.planner import (
    EntityMention,
    GraphOperation,
    QueryPlan,
    QueryPlanProposal,
    Route,
    analyze_query,
    merge_query_plan,
)
from backend.tests.test_graph_tools import SNAPSHOT
from backend.tests.test_orchestrator import _tools


def test_structural_question_keeps_exact_and_graph_routes() -> None:
    plan = analyze_query("Who are the callers of verify_token within 4 hops?")
    assert Route.EXACT in plan.routes
    assert Route.GRAPH in plan.routes
    assert Route.FULL_TEXT in plan.routes
    assert GraphOperation.CALLERS in plan.graph_operations
    assert plan.hop_limit == 4
    assert [mention.text for mention in plan.entity_mentions] == ["verify_token"]


def test_what_calls_question_routes_to_callers_only() -> None:
    plan = analyze_query("What calls src.click.core.Command.make_context?")
    assert GraphOperation.CALLERS in plan.graph_operations
    assert GraphOperation.CALLEES not in plan.graph_operations


def test_required_waypoint_preserves_endpoint_mention_order() -> None:
    plan = analyze_query("Find paths from `a.caller` to `b.callee` through NoSuchWidget")
    assert [mention.text for mention in plan.entity_mentions] == [
        "a.caller",
        "b.callee",
        "NoSuchWidget",
    ]
    assert plan.required_waypoints == ["NoSuchWidget"]


def test_conceptual_question_uses_text_and_vector_routes() -> None:
    plan = analyze_query("How does request authorization work?")
    assert plan.routes == [Route.FULL_TEXT, Route.VECTOR]
    assert plan.entity_mentions == []


def test_hop_limit_is_bounded_and_selected_context_is_preserved() -> None:
    plan = analyze_query(
        "Find paths from module.auth to module.api within 99 hops",
        selected_site_id="site_123",
    )
    assert plan.hop_limit == 5
    assert {mention.text for mention in plan.entity_mentions} == {"module.auth", "module.api"}
    assert plan.selected_site_id == "site_123"


def test_plan_api_rejects_empty_question_and_returns_typed_routes() -> None:
    try:
        query_plan(QueryRequest(question="  "))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("empty question should be rejected")

    response = query_plan(QueryRequest(question="callers of verify_token"))
    assert "GRAPH" in {route.value for route in response.routes}


def test_routing_dataset_has_schema_validity_and_mandatory_route_recall() -> None:
    path = Path("docs/evaluation/query-routing-m18.json")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    examples = dataset["examples"]
    assert dataset["dataset_id"] == "query-routing-m18-v1"
    assert len(examples) >= 150
    categories = {item["intent"] for item in examples}
    assert categories == {
        "exact_identifier",
        "caller",
        "reference",
        "inheritance",
        "impact",
        "semantic",
        "hybrid",
        "git",
        "runtime_test",
        "diff",
        "selected_node_followup",
        "selected_site_followup",
        "irrelevant_out_of_scope",
    }

    valid = 0
    expected_total = 0
    retained_total = 0
    for item in examples:
        plan = analyze_query(
            item["question"],
            snapshot_id="snap-routing-eval",
            selected_node_id=item.get("selected_node_id"),
            selected_site_id=item.get("selected_site_id"),
        )
        QueryPlan.model_validate(plan.model_dump())
        valid += 1
        mandatory = set(item["mandatory_routes"])
        actual = {route.value for route in plan.routes}
        expected_total += len(mandatory)
        retained_total += len(mandatory & actual)
        proposal = QueryPlanProposal()
        merged = merge_query_plan(
            plan, proposal, question=item["question"], snapshot_id="snap-routing-eval"
        )
        assert mandatory <= {route.value for route in merged.routes}
    assert valid / len(examples) == 1.0
    assert retained_total / expected_total >= 0.98


def test_independent_operation_evaluation_checks_tools_arguments_and_negatives() -> None:
    dataset = json.loads(
        Path("docs/evaluation/query-operations-post-m20.json").read_text(encoding="utf-8")
    )
    cases = dataset["cases"]
    assert dataset["dataset_id"] == "query-operations-post-m20-v1"
    categories = {item["category"] for item in cases}
    assert {
        "callers",
        "callees",
        "call_sites",
        "definitions",
        "imports",
        "inheritance",
        "overrides",
        "dispatch",
        "paths",
        "dispatch_paths",
        "runtime_tests",
        "retrieval",
        "selected_site_followup",
        "negative_selected_site",
        "negative",
    } <= categories

    passed = 0
    failures: list[str] = []
    for case in cases:
        result = asyncio.run(
            QueryOrchestrator(_tools()).execute(
                case["question"],
                snapshot_id=SNAPSHOT,
                selected_site_id=case.get("selected_site_id"),
            )
        )
        actual_routes = {route.value for route in result.plan.routes}
        actual_calls = [
            execution.request.model_dump(mode="json", exclude_none=True)
            for execution in result.tool_executions
            if execution.request is not None
        ]
        ok = set(case["expected_routes"]) <= actual_routes
        ok &= result.plan.selected_site_id == case.get("selected_site_id")
        for expected_call in case["expected_calls"]:
            ok &= any(
                all(actual.get(key) == expected for key, expected in expected_call.items())
                for actual in actual_calls
            )
        forbidden_tools = set(case.get("forbidden_tools", ()))
        ok &= not any(call["tool"] in forbidden_tools for call in actual_calls)
        expected_mode = case.get("expected_mode")
        if expected_mode:
            relevant = [
                call
                for call in actual_calls
                if call["tool"] in {"find_paths", "find_dispatch_candidates"}
            ]
            ok &= any(call.get("mode") == expected_mode for call in relevant)
        if case["id"] == "selected-unresolved-site":
            unresolved = [
                execution
                for execution in result.tool_executions
                if execution.result.tool == "find_callees"
            ]
            ok &= len(unresolved) == 1 and unresolved[0].result.results == ()
        if ok:
            passed += 1
        else:
            failures.append(case["id"])

    assert passed == len(cases), f"operation evaluation {passed}/{len(cases)}; failures={failures}"


def test_plan_schema_rejects_unknown_fields_routes_and_oversized_mentions() -> None:
    with pytest.raises(ValidationError):
        RouteValidator.parse({"routes": ["GRAPH"], "tool": "os.system"})
    with pytest.raises(ValidationError):
        RouteValidator.parse({"routes": ["SHELL"], "graph_operations": []})
    with pytest.raises(ValidationError):
        RouteValidator.parse(
            {
                "entity_mentions": [EntityMention(text="x" * 161).model_dump()],
                "routes": [],
            }
        )


def test_model_proposal_cannot_remove_required_graph_or_exact_routes() -> None:
    question = "Who are the callers of verify_token?"
    deterministic = analyze_query(question, snapshot_id="snap-routing")
    merged = merge_query_plan(
        deterministic,
        QueryPlanProposal(routes=[Route.VECTOR]),
        question=question,
        snapshot_id="snap-routing",
    )
    assert {Route.EXACT, Route.GRAPH, Route.VECTOR} <= set(merged.routes)
    assert merged.snapshot_id == "snap-routing"
    assert merged.graph_operations == deterministic.graph_operations

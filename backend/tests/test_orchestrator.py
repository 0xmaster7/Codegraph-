from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.fusion import EvidenceType, normalize_graph_evidence
from backend.app.graph_tools import DeterministicGraphTools, GraphToolResult
from backend.app.investigation import build_investigation_response
from backend.app.orchestrator import (
    GraphToolCall,
    GraphToolRegistry,
    LLMProviderCompletion,
    LLMQueryAnalyzer,
    LLMUsage,
    QueryBudgetError,
    QueryBudgetGuard,
    QueryOrchestrator,
    QueryToolExecution,
    RouteValidator,
    ToolName,
    _build_evidence_pool,
)
from backend.app.planner import Route
from backend.tests.test_graph_tools import SNAPSHOT, _analysis


class FakeProvider:
    provider_id = "fake-local"
    model_id = "routing-test-double"
    hosted = False

    def __init__(self, response: object, *, delay: float = 0) -> None:
        self.response = response
        self.delay = delay
        self.calls = 0

    def estimate_query_cost(self, _question: str) -> Decimal | None:
        return None

    async def analyze_query(self, _question: str) -> object:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


class FakeHostedProvider(FakeProvider):
    provider_id = "fake-hosted"
    model_id = "budget-test-double"
    hosted = True

    def __init__(self, response: object, estimate: Decimal | None) -> None:
        super().__init__(response)
        self.estimate = estimate

    def estimate_query_cost(self, _question: str) -> Decimal | None:
        return self.estimate


def _search_records(analysis: object) -> list[dict[str, object]]:
    return [
        {**item.model_dump(mode="json"), "score": 0.8}
        for item in analysis.retrieval_chunks  # type: ignore[attr-defined]
    ]


def _tools() -> DeterministicGraphTools:
    analysis = _analysis()
    records = _search_records(analysis)
    return DeterministicGraphTools(
        analysis,
        runtime_records=[
            {
                "snapshot_id": SNAPSHOT,
                "test_definition_id": "test-caller",
                "test_invocation_id": "invocation-caller",
                "entity_id": "caller",
                "run_id": "run-1",
                "entry_id": "entry-1",
            }
        ],
        lexical_search=lambda _query, limit: records[:limit],
        semantic_search=lambda _query, limit: records[:limit],
    )


def test_route_validator_rejects_arbitrary_tool_names_and_fields() -> None:
    with pytest.raises(ValidationError):
        GraphToolCall.model_validate(
            {"tool": "python.exec", "snapshot_id": SNAPSHOT, "query": "anything"}
        )
    with pytest.raises(ValidationError):
        GraphToolCall.model_validate(
            {
                "tool": ToolName.SEARCH_CODE_TEXT,
                "snapshot_id": SNAPSHOT,
                "query": "text",
                "shell_command": "echo unsafe",
            }
        )
    with pytest.raises(ValidationError):
        GraphToolCall(tool=ToolName.FIND_CALLERS, snapshot_id=SNAPSHOT)
    proposal = RouteValidator.parse({"routes": ["VECTOR"], "hop_limit": 5})
    assert proposal.routes == [Route.VECTOR]
    with pytest.raises(ValidationError):
        GraphToolCall(
            tool=ToolName.FIND_CHANGED_PATHS,
            snapshot_id=SNAPSHOT,
            commit_id="HEAD; MATCH (n) DETACH DELETE n",
        )


def test_graph_tool_registry_dispatches_only_allowlisted_typed_tools() -> None:
    tools = _tools()
    registry = GraphToolRegistry(tools)
    result = registry.invoke(
        GraphToolCall(
            tool=ToolName.RESOLVE_SYMBOL,
            snapshot_id=SNAPSHOT,
            query="caller",
        )
    )
    assert result.tool == "resolve_symbol"
    assert result.results[0]["id"] == "caller"
    with pytest.raises(KeyError, match="snapshot"):
        registry.invoke(
            GraphToolCall(
                tool=ToolName.RESOLVE_SYMBOL,
                snapshot_id="snap-other",
                query="caller",
            )
        )


def test_optional_provider_can_only_add_routes_and_malformed_output_falls_back() -> None:
    question = "Who are the callers of verify_token?"
    provider = FakeProvider({"routes": ["VECTOR"], "graph_operations": []})
    result = asyncio.run(LLMQueryAnalyzer(provider).analyze(question, snapshot_id=SNAPSHOT))
    assert result.used_provider
    assert {Route.EXACT, Route.GRAPH, Route.VECTOR} <= set(result.plan.routes)
    assert result.plan.graph_operations
    assert result.plan.snapshot_id == SNAPSHOT

    malformed = FakeProvider({"routes": ["SHELL"], "command": "cat /etc/passwd"})
    rejected = asyncio.run(LLMQueryAnalyzer(malformed).analyze(question, snapshot_id=SNAPSHOT))
    assert not rejected.used_provider
    assert Route.GRAPH in rejected.plan.routes
    assert any("rejected" in item for item in rejected.diagnostics)
    assert malformed.calls == 1


def test_analyzer_timeout_falls_back_to_deterministic_plan() -> None:
    question = "Who are the callers of verify_token?"
    provider = FakeProvider({"routes": ["VECTOR"]}, delay=0.05)
    result = asyncio.run(
        LLMQueryAnalyzer(provider, timeout_seconds=0.01).analyze(question, snapshot_id=SNAPSHOT)
    )
    assert not result.used_provider
    assert Route.GRAPH in result.plan.routes
    assert any("timed out" in item for item in result.diagnostics)


def test_hosted_provider_is_never_called_without_explicit_policy_or_budget() -> None:
    provider = FakeHostedProvider({"routes": ["VECTOR"]}, Decimal("0.01"))
    no_opt_in = asyncio.run(
        LLMQueryAnalyzer(provider, hosted_enabled=True).analyze(
            "How does authorization work?", snapshot_id=SNAPSHOT
        )
    )
    assert not no_opt_in.used_provider
    assert provider.calls == 0

    guard = QueryBudgetGuard(max_single_query_usd=Decimal("0.01"), max_total_usd=Decimal("0.05"))
    over_budget_provider = FakeHostedProvider({"routes": ["VECTOR"]}, Decimal("0.02"))
    denied = asyncio.run(
        LLMQueryAnalyzer(
            over_budget_provider,
            hosted_enabled=True,
            hosted_transmission_approved=True,
            budget_guard=guard,
        ).analyze("How does authorization work?", snapshot_id=SNAPSHOT)
    )
    assert not denied.used_provider
    assert any("BUDGET_EXCEEDED" in item for item in denied.diagnostics)
    assert over_budget_provider.calls == 0


def test_hosted_invalid_proposal_retains_post_response_usage_accounting() -> None:
    provider = FakeHostedProvider(
        LLMProviderCompletion(
            proposal={"routes": ["SHELL"]},
            usage=LLMUsage(
                input_tokens=12,
                cached_input_tokens=0,
                output_tokens=3,
                estimated_cost_usd=Decimal("0.01"),
                actual_cost_usd=Decimal("0.012"),
                latency_ms=32,
            ),
        ),
        Decimal("0.01"),
    )
    guard = QueryBudgetGuard(max_single_query_usd=Decimal("0.05"), max_total_usd=Decimal("0.10"))
    result = asyncio.run(
        LLMQueryAnalyzer(
            provider,
            hosted_enabled=True,
            hosted_transmission_approved=True,
            budget_guard=guard,
        ).analyze("How is authorization checked?", snapshot_id=SNAPSHOT)
    )
    assert not result.used_provider
    assert result.actual_cost_usd == Decimal("0.012")
    assert result.input_tokens == 12
    assert result.output_tokens == 3
    assert result.cache_hit is False
    assert provider.calls == 1
    assert any("proposal was rejected" in diagnostic for diagnostic in result.diagnostics)


def test_budget_guard_allows_exact_boundary_and_rejects_missing_pricing() -> None:
    guard = QueryBudgetGuard(max_single_query_usd=Decimal("0.02"), max_total_usd=Decimal("0.02"))
    assert asyncio.run(guard.authorize(Decimal("0.02"))) == Decimal("0.02")
    with pytest.raises(QueryBudgetError, match="BUDGET_EXCEEDED"):
        asyncio.run(guard.authorize(Decimal("0.0001")))
    with pytest.raises(QueryBudgetError, match="unavailable"):
        asyncio.run(guard.authorize(None))
    invalid = QueryBudgetGuard(max_single_query_usd=Decimal("NaN"), max_total_usd=Decimal("1.00"))
    with pytest.raises(QueryBudgetError, match="invalid"):
        asyncio.run(invalid.authorize(Decimal("0")))


def test_orchestrator_keeps_graph_runtime_and_passages_distinct() -> None:
    provider = FakeProvider({"routes": ["VECTOR"]})
    orchestrator = QueryOrchestrator(_tools(), LLMQueryAnalyzer(provider))
    result = asyncio.run(
        orchestrator.execute(
            "Which tests observed callers of `callee` at `caller`?",
            snapshot_id=SNAPSHOT,
        )
    )
    routes = {execution.route for execution in result.tool_executions}
    assert {Route.EXACT, Route.GRAPH, Route.FULL_TEXT, Route.VECTOR, Route.RUNTIME} <= routes
    evidence_types = {item.item.type for item in result.evidence_pool.items}
    assert EvidenceType.STATIC_BINDING in evidence_types
    assert EvidenceType.LEXICAL_PASSAGE in evidence_types
    assert EvidenceType.SEMANTIC_PASSAGE in evidence_types
    assert EvidenceType.RUNTIME_OBSERVATION in evidence_types
    assert all(item.item.snapshot_id == SNAPSHOT for item in result.evidence_pool.items)
    assert all(
        occurrence.verified_graph_path is None
        for item in result.evidence_pool.items
        if item.item.type in {EvidenceType.LEXICAL_PASSAGE, EvidenceType.SEMANTIC_PASSAGE}
        for occurrence in item.occurrences
    )
    assert result.evidence_pool.conflicting_evidence_ids == ()


def test_path_orchestration_preserves_verified_path_and_snapshot_checks() -> None:
    result = asyncio.run(
        QueryOrchestrator(_tools()).execute(
            "Find paths from `caller` to `callee`",
            snapshot_id=SNAPSHOT,
        )
    )
    path_calls = [
        execution
        for execution in result.tool_executions
        if execution.request and execution.request.tool == ToolName.FIND_PATHS
    ]
    assert path_calls
    assert path_calls[0].result.results[0]["node_ids"] == ["caller", "callee"]
    assert any(
        occurrence.verified_graph_path is True
        for item in result.evidence_pool.items
        for occurrence in item.occurrences
    )
    with pytest.raises(KeyError, match="snapshot"):
        asyncio.run(
            QueryOrchestrator(_tools()).execute("callers of caller", snapshot_id="snap-other")
        )


@pytest.mark.parametrize(
    ("question", "has_path", "diagnostic"),
    [
        ("Find paths from `a` to `b.callee` through NoSuchWidget", False, "not uniquely resolved"),
        ("Find paths from `a` to `b.callee` through `b.Base`", False, "No graph path satisfies"),
        ("Find paths from `a` to `b.callee` through `a.caller`", True, ""),
        ("Find paths from `a` to NoSuchWidget through `a.caller`", False, "not uniquely resolved"),
        ("Find paths from `a` to `b.callee` through `run`", False, "not uniquely resolved"),
    ],
)
def test_required_waypoint_is_enforced_before_path_claims(
    question: str, has_path: bool, diagnostic: str
) -> None:
    tools = _tools()
    result = asyncio.run(QueryOrchestrator(tools).execute(question, snapshot_id=SNAPSHOT))
    path_results = [
        path
        for execution in result.tool_executions
        if execution.result.tool == ToolName.FIND_PATHS.value
        for path in execution.result.results
    ]
    assert bool(path_results) is has_path
    if has_path:
        assert all("caller" in path["node_ids"][1:-1] for path in path_results)
    else:
        assert any(diagnostic in item for item in result.diagnostics)
    investigation = build_investigation_response(result, tools)
    assert bool(investigation.evidence_paths) is has_path
    assert (
        any("verified graph path" in item for item in investigation.answer.rendered_text)
        is has_path
    )


def test_evidence_pool_rejects_cross_snapshot_and_preserves_conflicts() -> None:
    first = normalize_graph_evidence(
        evidence_type=EvidenceType.STATIC_BINDING,
        snapshot_id=SNAPSHOT,
        source_record_id="edge-shared",
        display_label="caller binds to first",
        provenance_pointer="prov-first",
    )
    conflicting = first.model_copy(update={"display_label": "caller binds to other"})
    foreign = first.model_copy(update={"snapshot_id": "snap-other"})
    execution = QueryToolExecution(
        route=Route.GRAPH,
        request=None,
        result=GraphToolResult(
            snapshot_id=SNAPSHOT,
            tool="find_callers",
            results=(
                {"evidence": first.model_dump(mode="json")},
                {"evidence": conflicting.model_dump(mode="json")},
                {"evidence": foreign.model_dump(mode="json")},
            ),
            result_limit=10,
            candidate_count_before_limit=3,
        ),
    )
    pool, diagnostics = _build_evidence_pool(SNAPSHOT, [execution])
    assert pool.conflicting_evidence_ids == (first.evidence_id,)
    assert len(pool.items) == 2
    assert all(item.conflicting_identity for item in pool.items)
    assert all(item.item.snapshot_id == SNAPSHOT for item in pool.items)
    assert any("cross-snapshot" in diagnostic for diagnostic in diagnostics)
    assert any("Conflicting records" in diagnostic for diagnostic in diagnostics)


def test_orchestration_call_bounds_are_enforced() -> None:
    orchestrator = QueryOrchestrator(_tools(), max_tool_calls=1)
    result = asyncio.run(
        orchestrator.execute(
            "Who are the callers of caller?",
            snapshot_id=SNAPSHOT,
        )
    )
    assert len(result.tool_executions) == 1
    assert result.truncated
    assert any("tool-call bound" in item for item in result.diagnostics)


def test_identical_snapshot_query_produces_stable_plan_tool_and_evidence_order() -> None:
    orchestrator = QueryOrchestrator(_tools())
    question = "Find paths from `caller` to `callee`"
    first = asyncio.run(orchestrator.execute(question, snapshot_id=SNAPSHOT))
    second = asyncio.run(orchestrator.execute(question, snapshot_id=SNAPSHOT))
    assert first.plan.model_dump(mode="json") == second.plan.model_dump(mode="json")
    assert [
        (item.route, item.operation, item.request, item.result.tool, item.result.results)
        for item in first.tool_executions
    ] == [
        (item.route, item.operation, item.request, item.result.tool, item.result.results)
        for item in second.tool_executions
    ]
    assert [item.model_dump(mode="json") for item in first.evidence_pool.items] == [
        item.model_dump(mode="json") for item in second.evidence_pool.items
    ]

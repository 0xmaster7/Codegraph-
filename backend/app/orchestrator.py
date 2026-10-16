from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fusion import EvidenceItem, EvidenceType, normalize_graph_evidence
from .graph_tools import (
    DeterministicGraphTools,
    GraphToolRequest,
    GraphToolResult,
    PathToolRequest,
)
from .ids import stable_id
from .models import SnapshotStatus
from .planner import (
    GraphOperation,
    QueryPlan,
    QueryPlanProposal,
    Route,
    analyze_query,
    merge_query_plan,
)

MAX_ORCHESTRATION_TOOL_CALLS = 32
MAX_QUERY_RESULT_LIMIT = 100
MAX_QUERY_EVIDENCE_ITEMS = 2_000
MAX_QUERY_PLANNER_TIMEOUT_SECONDS = 8.0


class LLMProvider(Protocol):
    """Optional query-analysis provider; it can propose plans but never invoke tools."""

    provider_id: str
    model_id: str
    hosted: bool

    def estimate_query_cost(self, question: str) -> Decimal | None: ...

    async def analyze_query(self, question: str) -> object: ...


class QueryBudgetError(ValueError):
    pass


class QueryBudgetGuard:
    """Fail-closed per-query and process-total cost preflight for optional LLM calls."""

    def __init__(
        self,
        *,
        max_single_query_usd: Decimal | None,
        max_total_usd: Decimal | None,
    ) -> None:
        self.max_single_query_usd = max_single_query_usd
        self.max_total_usd = max_total_usd
        self._estimated_reserved_usd = Decimal("0")
        self._lock = asyncio.Lock()

    @staticmethod
    def _validated_estimate(value: Decimal | None) -> Decimal:
        if value is None:
            raise QueryBudgetError("LLM pricing and conservative token estimation are unavailable")
        try:
            estimate = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise QueryBudgetError("LLM cost estimate is invalid") from exc
        if not estimate.is_finite() or estimate < 0:
            raise QueryBudgetError("LLM cost estimate is invalid")
        return estimate

    async def authorize(self, estimated_cost_usd: Decimal | None) -> Decimal:
        estimate = self._validated_estimate(estimated_cost_usd)
        if self.max_single_query_usd is None or self.max_total_usd is None:
            raise QueryBudgetError("LLM per-query and total budget limits must be configured")
        max_single_query_usd = self.max_single_query_usd
        max_total_usd = self.max_total_usd
        if (
            not max_single_query_usd.is_finite()
            or not max_total_usd.is_finite()
            or max_single_query_usd < 0
            or max_total_usd < 0
        ):
            raise QueryBudgetError("LLM budget limits are invalid")
        async with self._lock:
            if estimate > Decimal(max_single_query_usd):
                raise QueryBudgetError("BUDGET_EXCEEDED: query exceeds its configured cost limit")
            if self._estimated_reserved_usd + estimate > Decimal(max_total_usd):
                raise QueryBudgetError(
                    "BUDGET_EXCEEDED: total configured LLM budget would be exceeded"
                )
            self._estimated_reserved_usd += estimate
        return estimate

    async def record_actual(self, estimate: Decimal, actual_cost_usd: Decimal) -> bool:
        actual = self._validated_estimate(actual_cost_usd)
        async with self._lock:
            self._estimated_reserved_usd = (
                max(Decimal("0"), self._estimated_reserved_usd - estimate) + actual
            )
            return (
                self.max_single_query_usd is not None
                and self.max_total_usd is not None
                and actual <= self.max_single_query_usd
                and self._estimated_reserved_usd <= self.max_total_usd
            )


class QueryAnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: QueryPlan
    provider_id: str | None = None
    model_id: str | None = None
    used_provider: bool = False
    estimated_cost_usd: Decimal | None = None
    actual_cost_usd: Decimal | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    cache_hit: bool | None = None
    diagnostics: tuple[str, ...] = ()


class LLMUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    actual_cost_usd: Decimal = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cache_hit: bool = False


class LLMProviderCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    proposal: Any
    usage: LLMUsage


class RouteValidator:
    """Validates provider output against closed plan enums and bounded fields."""

    @staticmethod
    def parse(raw: object) -> QueryPlanProposal:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return QueryPlanProposal.model_validate(raw)

    @staticmethod
    def merge(
        deterministic: QueryPlan,
        proposal: QueryPlanProposal,
        *,
        question: str,
        snapshot_id: str,
    ) -> QueryPlan:
        return merge_query_plan(
            deterministic,
            proposal,
            question=question,
            snapshot_id=snapshot_id,
        )


class LLMQueryAnalyzer:
    """Merges a validated optional provider proposal into deterministic pre-rules."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        hosted_enabled: bool = False,
        hosted_transmission_approved: bool = False,
        budget_guard: QueryBudgetGuard | None = None,
        timeout_seconds: float = MAX_QUERY_PLANNER_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < timeout_seconds <= MAX_QUERY_PLANNER_TIMEOUT_SECONDS:
            raise ValueError("Query analyzer timeout must be between 0 and 8 seconds")
        self.provider = provider
        self.hosted_enabled = hosted_enabled
        self.hosted_transmission_approved = hosted_transmission_approved
        self.budget_guard = budget_guard
        self.timeout_seconds = timeout_seconds

    async def analyze(
        self,
        question: str,
        *,
        snapshot_id: str,
        selected_node_id: str | None = None,
        selected_edge_id: str | None = None,
        selected_site_id: str | None = None,
    ) -> QueryAnalysisResult:
        mandatory = analyze_query(
            question,
            snapshot_id=snapshot_id,
            selected_node_id=selected_node_id,
            selected_edge_id=selected_edge_id,
            selected_site_id=selected_site_id,
        )
        provider = self.provider
        if provider is None:
            return QueryAnalysisResult(
                plan=mandatory,
                diagnostics=("No LLM provider is configured; deterministic pre-rules were used.",),
            )
        estimated_cost: Decimal | None = None
        reservation = Decimal("0")
        usage: LLMUsage | None = None
        actual_cost: Decimal | None = None
        if provider.hosted:
            if not self.hosted_enabled or not self.hosted_transmission_approved:
                return QueryAnalysisResult(
                    plan=mandatory,
                    provider_id=provider.provider_id,
                    model_id=provider.model_id,
                    diagnostics=(
                        "Hosted analysis is disabled or lacks explicit transmission approval.",
                    ),
                )
            if self.budget_guard is None:
                return QueryAnalysisResult(
                    plan=mandatory,
                    provider_id=provider.provider_id,
                    model_id=provider.model_id,
                    diagnostics=("Hosted query analysis has no configured budget guard.",),
                )
            try:
                estimated_cost = provider.estimate_query_cost(question)
                reservation = await self.budget_guard.authorize(estimated_cost)
            except QueryBudgetError as exc:
                return QueryAnalysisResult(
                    plan=mandatory,
                    provider_id=provider.provider_id,
                    model_id=provider.model_id,
                    diagnostics=(str(exc),),
                )
        else:
            estimated_cost = provider.estimate_query_cost(question)
        try:
            raw = await asyncio.wait_for(
                provider.analyze_query(question), timeout=self.timeout_seconds
            )
            if isinstance(raw, LLMProviderCompletion):
                usage = raw.usage
                actual_cost = usage.actual_cost_usd
                if provider.hosted:
                    assert self.budget_guard is not None
                    within_budget = await self.budget_guard.record_actual(reservation, actual_cost)
                    if not within_budget:
                        return QueryAnalysisResult(
                            plan=mandatory,
                            provider_id=provider.provider_id,
                            model_id=provider.model_id,
                            estimated_cost_usd=estimated_cost,
                            actual_cost_usd=actual_cost,
                            input_tokens=usage.input_tokens,
                            cached_input_tokens=usage.cached_input_tokens,
                            output_tokens=usage.output_tokens,
                            latency_ms=usage.latency_ms,
                            cache_hit=usage.cache_hit,
                            diagnostics=(
                                "BUDGET_EXCEEDED: provider usage exceeded its preflight budget; "
                                "deterministic pre-rules were used.",
                            ),
                        )
                raw = raw.proposal
            elif provider.hosted:
                raise ValueError("Hosted provider response omitted required usage accounting")
            proposal = RouteValidator.parse(raw)
            merged = RouteValidator.merge(
                mandatory, proposal, question=question, snapshot_id=snapshot_id
            )
        except TimeoutError:
            return QueryAnalysisResult(
                plan=mandatory,
                provider_id=provider.provider_id,
                model_id=provider.model_id,
                estimated_cost_usd=estimated_cost,
                diagnostics=("Query analyzer timed out; deterministic pre-rules were used.",),
            )
        except Exception as exc:
            return QueryAnalysisResult(
                plan=mandatory,
                provider_id=provider.provider_id,
                model_id=provider.model_id,
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=actual_cost,
                input_tokens=usage.input_tokens if usage else None,
                cached_input_tokens=usage.cached_input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                latency_ms=usage.latency_ms if usage else None,
                cache_hit=usage.cache_hit if usage else None,
                diagnostics=(
                    f"Query analyzer proposal was rejected ({type(exc).__name__}); "
                    "deterministic pre-rules were used.",
                ),
            )
        return QueryAnalysisResult(
            plan=merged,
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            used_provider=True,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
            input_tokens=usage.input_tokens if usage else None,
            cached_input_tokens=usage.cached_input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            latency_ms=usage.latency_ms if usage else None,
            cache_hit=usage.cache_hit if usage else None,
        )


class ToolName(StrEnum):
    RESOLVE_SYMBOL = "resolve_symbol"
    GET_SOURCE_SPAN = "get_source_span"
    FIND_REFERENCES = "find_references"
    FIND_CALL_SITES = "find_call_sites"
    FIND_CALLERS = "find_callers"
    FIND_CALLEES = "find_callees"
    FIND_IMPORTERS = "find_importers"
    FIND_INHERITANCE = "find_inheritance"
    FIND_OVERRIDES = "find_overrides"
    FIND_DISPATCH_CANDIDATES = "find_dispatch_candidates"
    FIND_PATHS = "find_paths"
    FIND_SHORTEST_EVIDENCE_PATH = "find_shortest_evidence_path"
    EXPAND_NEIGHBORHOOD = "expand_neighborhood"
    GET_EDGE_PROVENANCE = "get_edge_provenance"
    REPLAY_EVIDENCE = "replay_evidence"
    GET_GIT_HISTORY = "get_git_history"
    FIND_CHANGED_PATHS = "find_changed_paths"
    FIND_CHANGED_SYMBOLS = "find_changed_symbols"
    FIND_OBSERVED_TESTS = "find_observed_tests"
    BUILD_TEST_PLAN = "build_test_plan"
    SEARCH_CODE_TEXT = "search_code_text"
    SEARCH_SEMANTIC_CHUNKS = "search_semantic_chunks"


class GraphToolCall(BaseModel):
    """Typed request accepted by the fixed M17 tool registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: ToolName
    snapshot_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=50, ge=1, le=MAX_QUERY_RESULT_LIMIT)
    query: str | None = Field(default=None, max_length=4096)
    entity_id: str | None = Field(default=None, max_length=160)
    entity_ids: tuple[str, ...] = Field(default=(), max_length=32)
    target_id: str | None = Field(default=None, max_length=160)
    site_id: str | None = Field(default=None, max_length=160)
    edge_id: str | None = Field(default=None, max_length=160)
    source_id: str | None = Field(default=None, max_length=160)
    destination_id: str | None = Field(default=None, max_length=160)
    file_path: str | None = Field(default=None, max_length=1024)
    start_byte: int | None = Field(default=None, ge=0)
    end_byte: int | None = Field(default=None, ge=0)
    commit_id: str | None = Field(
        default=None, min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]+$"
    )
    hop_limit: int = Field(default=3, ge=1, le=5)
    mode: str = "STRICT_STATIC_BINDING"

    @model_validator(mode="after")
    def validate_tool_arguments(self) -> GraphToolCall:
        required: dict[ToolName, tuple[str, ...]] = {
            ToolName.GET_SOURCE_SPAN: ("file_path", "start_byte", "end_byte"),
            ToolName.FIND_REFERENCES: ("target_id",),
            ToolName.FIND_CALLERS: ("target_id",),
            ToolName.FIND_CALLEES: ("site_id",),
            ToolName.FIND_IMPORTERS: ("target_id",),
            ToolName.FIND_DISPATCH_CANDIDATES: ("site_id",),
            ToolName.FIND_PATHS: ("source_id", "destination_id"),
            ToolName.FIND_SHORTEST_EVIDENCE_PATH: ("source_id", "destination_id"),
            ToolName.EXPAND_NEIGHBORHOOD: ("entity_id",),
            ToolName.GET_EDGE_PROVENANCE: ("edge_id",),
            ToolName.REPLAY_EVIDENCE: ("edge_id",),
            ToolName.FIND_CHANGED_PATHS: (),
            ToolName.FIND_CHANGED_SYMBOLS: (),
            ToolName.FIND_OBSERVED_TESTS: (),
            ToolName.BUILD_TEST_PLAN: (),
            ToolName.SEARCH_CODE_TEXT: ("query",),
            ToolName.SEARCH_SEMANTIC_CHUNKS: ("query",),
            ToolName.RESOLVE_SYMBOL: ("query",),
            ToolName.FIND_CALL_SITES: (),
            ToolName.FIND_INHERITANCE: (),
            ToolName.FIND_OVERRIDES: (),
            ToolName.GET_GIT_HISTORY: (),
        }
        values = self.model_dump()
        missing = [name for name in required[self.tool] if values.get(name) is None]
        if missing:
            raise ValueError(
                f"{self.tool.value} is missing required field(s): {', '.join(missing)}"
            )
        if self.mode not in {"STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"}:
            raise ValueError("Unsupported graph traversal mode")
        if self.start_byte is not None and self.end_byte is not None:
            if self.end_byte < self.start_byte:
                raise ValueError("Source span end must not precede its start")
        return self


class GraphToolRegistry:
    """Fixed allowlist dispatcher. No attribute lookup or caller-provided function names."""

    def __init__(self, tools: DeterministicGraphTools) -> None:
        self.tools = tools

    def invoke(self, call: GraphToolCall) -> GraphToolResult:
        if call.snapshot_id != self.tools.snapshot_id:
            raise KeyError("Tool request snapshot does not match the loaded analysis")
        request = GraphToolRequest(snapshot_id=call.snapshot_id, limit=call.limit)
        match call.tool:
            case ToolName.RESOLVE_SYMBOL:
                return self.tools.resolve_symbol(request, call.query or "")
            case ToolName.GET_SOURCE_SPAN:
                return self.tools.get_source_span(
                    request, call.file_path or "", call.start_byte or 0, call.end_byte or 0
                )
            case ToolName.FIND_REFERENCES:
                return self.tools.find_references(request, call.target_id or "")
            case ToolName.FIND_CALL_SITES:
                return self.tools.find_call_sites(
                    request, context_id=call.entity_id, query=call.query
                )
            case ToolName.FIND_CALLERS:
                return self.tools.find_callers(request, call.target_id or "")
            case ToolName.FIND_CALLEES:
                return self.tools.find_callees(request, call.site_id or "")
            case ToolName.FIND_IMPORTERS:
                return self.tools.find_importers(request, call.target_id or "")
            case ToolName.FIND_INHERITANCE:
                return self.tools.find_inheritance(request, call.entity_id)
            case ToolName.FIND_OVERRIDES:
                return self.tools.find_overrides(request, call.entity_id)
            case ToolName.FIND_DISPATCH_CANDIDATES:
                return self.tools.find_dispatch_candidates(request, call.site_id or "")
            case ToolName.FIND_PATHS:
                return self.tools.find_paths(
                    PathToolRequest(
                        snapshot_id=call.snapshot_id,
                        limit=call.limit,
                        source_id=call.source_id or "",
                        target_id=call.destination_id or "",
                        mode=call.mode,  # type: ignore[arg-type]
                        hop_limit=call.hop_limit,
                    )
                )
            case ToolName.FIND_SHORTEST_EVIDENCE_PATH:
                return self.tools.find_shortest_evidence_path(
                    PathToolRequest(
                        snapshot_id=call.snapshot_id,
                        limit=call.limit,
                        source_id=call.source_id or "",
                        target_id=call.destination_id or "",
                        mode=call.mode,  # type: ignore[arg-type]
                        hop_limit=call.hop_limit,
                    )
                )
            case ToolName.EXPAND_NEIGHBORHOOD:
                return self.tools.expand_neighborhood(
                    request,
                    call.entity_id or "",
                    hop_limit=call.hop_limit,
                    mode=call.mode,  # type: ignore[arg-type]
                )
            case ToolName.GET_EDGE_PROVENANCE:
                return self.tools.get_edge_provenance(request, call.edge_id or "")
            case ToolName.REPLAY_EVIDENCE:
                return self.tools.replay_evidence(request, call.edge_id or "")
            case ToolName.GET_GIT_HISTORY:
                return self.tools.get_git_history(request)
            case ToolName.FIND_CHANGED_PATHS:
                return self.tools.find_changed_paths(request, commit_id=call.commit_id)
            case ToolName.FIND_CHANGED_SYMBOLS:
                return self.tools.find_changed_symbols(request, commit_id=call.commit_id)
            case ToolName.FIND_OBSERVED_TESTS:
                return self.tools.find_observed_tests(request, entity_ids=call.entity_ids)
            case ToolName.BUILD_TEST_PLAN:
                return self.tools.build_test_plan(request, call.entity_ids)
            case ToolName.SEARCH_CODE_TEXT:
                return self.tools.search_code_text(request, call.query or "")
            case ToolName.SEARCH_SEMANTIC_CHUNKS:
                return self.tools.search_semantic_chunks(request, call.query or "")


class EvidenceOccurrence(BaseModel):
    model_config = ConfigDict(frozen=True)

    originating_tool: str
    result_index: int = Field(ge=0)
    uncertain: bool = False
    verified_graph_path: bool | None = None


class OrchestratedEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: EvidenceItem
    occurrences: tuple[EvidenceOccurrence, ...]
    conflicting_identity: bool = False


class QueryEvidencePool(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    items: tuple[OrchestratedEvidence, ...]
    conflicting_evidence_ids: tuple[str, ...] = ()


class QueryToolExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    route: Route
    operation: GraphOperation | None = None
    request: GraphToolCall | None
    result: GraphToolResult


class QueryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    plan: QueryPlan
    provider_id: str | None = None
    model_id: str | None = None
    estimated_cost_usd: Decimal | None = None
    actual_cost_usd: Decimal | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    cache_hit: bool | None = None
    tool_executions: tuple[QueryToolExecution, ...]
    evidence_pool: QueryEvidencePool
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()


class QueryOrchestrator:
    """Bounded deterministic executor for validated M17 tool calls."""

    def __init__(
        self,
        tools: DeterministicGraphTools,
        analyzer: LLMQueryAnalyzer | None = None,
        *,
        tool_limit: int = 50,
        max_tool_calls: int = MAX_ORCHESTRATION_TOOL_CALLS,
    ) -> None:
        if not 1 <= tool_limit <= MAX_QUERY_RESULT_LIMIT:
            raise ValueError("Orchestration result limit must be between 1 and 100")
        if not 1 <= max_tool_calls <= MAX_ORCHESTRATION_TOOL_CALLS:
            raise ValueError("Orchestration call bound must be between 1 and 32")
        self.tools = tools
        self.registry = GraphToolRegistry(tools)
        self.analyzer = analyzer or LLMQueryAnalyzer()
        self.tool_limit = tool_limit
        self.max_tool_calls = max_tool_calls

    async def execute(
        self,
        question: str,
        *,
        snapshot_id: str,
        selected_node_id: str | None = None,
        selected_edge_id: str | None = None,
        selected_site_id: str | None = None,
    ) -> QueryResponse:
        if not question.strip():
            raise ValueError("Question cannot be empty")
        if len(question) > 4096:
            raise ValueError("Question exceeds the 4096-character limit")
        if snapshot_id != self.tools.snapshot_id:
            raise KeyError("Query snapshot does not match the loaded analysis")
        if self.tools.analysis.status != SnapshotStatus.READY:
            raise KeyError("Only READY snapshots can be queried")

        analysis = await self.analyzer.analyze(
            question,
            snapshot_id=snapshot_id,
            selected_node_id=selected_node_id,
            selected_edge_id=selected_edge_id,
            selected_site_id=selected_site_id,
        )
        plan = analysis.plan
        executions: list[QueryToolExecution] = []
        diagnostics = list(analysis.diagnostics)
        resolved_ids: set[str] = set()
        resolved_order: list[str] = []
        resolved_mentions: dict[str, str] = {}
        truncated = False

        def invoke(
            route: Route, tool: ToolName, *, operation: GraphOperation | None = None, **args: Any
        ) -> GraphToolResult | None:
            nonlocal truncated
            if len(executions) >= self.max_tool_calls:
                truncated = True
                if "Orchestration reached its deterministic tool-call bound." not in diagnostics:
                    diagnostics.append("Orchestration reached its deterministic tool-call bound.")
                return None
            call = GraphToolCall(
                tool=tool,
                snapshot_id=snapshot_id,
                limit=self.tool_limit,
                **args,
            )
            result = self.registry.invoke(call)
            if result.snapshot_id != snapshot_id:
                raise KeyError("A graph tool returned evidence from a different snapshot")
            executions.append(
                QueryToolExecution(route=route, operation=operation, request=call, result=result)
            )
            diagnostics.extend(result.diagnostics)
            truncated = truncated or result.truncated
            return result

        if plan.snapshot_id != snapshot_id:
            raise KeyError("Validated query plan is not bound to the requested snapshot")

        for mention in plan.entity_mentions:
            result = invoke(Route.EXACT, ToolName.RESOLVE_SYMBOL, query=mention.text)
            if result is None:
                break
            for record in result.results:
                # M17's resolver reports an unambiguous symbol match as UNIQUE.
                # Preserve only that exact canonical identity for later graph calls.
                if record.get("resolution_status") == "UNIQUE" and record.get("id"):
                    entity_id = str(record["id"])
                    resolved_mentions[mention.text.casefold()] = entity_id
                    if entity_id not in resolved_ids:
                        resolved_ids.add(entity_id)
                        resolved_order.append(entity_id)

        if selected_node_id:
            if selected_node_id in self.tools.entities:
                if selected_node_id not in resolved_ids:
                    resolved_ids.add(selected_node_id)
                    resolved_order.append(selected_node_id)
            else:
                diagnostics.append("Selected node is not present in the requested snapshot.")
        if selected_site_id and selected_site_id not in self.tools.sites:
            diagnostics.append("Selected site is not present in the requested snapshot.")
        if selected_edge_id and selected_edge_id not in (
            self.tools.provenance_by_edge | self.tools.binding_by_edge | self.tools.derived_by_edge
        ):
            diagnostics.append("Selected edge is not present in the requested snapshot.")
        if selected_edge_id and selected_edge_id in (
            self.tools.provenance_by_edge | self.tools.binding_by_edge | self.tools.derived_by_edge
        ):
            invoke(Route.GRAPH, ToolName.GET_EDGE_PROVENANCE, edge_id=selected_edge_id)
        if selected_site_id and selected_site_id in self.tools.sites:
            context_id = self.tools.sites[selected_site_id].context_id
            if context_id not in resolved_ids:
                resolved_ids.add(context_id)
                resolved_order.append(context_id)

        # Mention order carries source/target intent for queries such as “path from A to B”.
        targets = resolved_order[:32]
        if Route.GRAPH in plan.routes:
            for operation in plan.graph_operations:
                if operation == GraphOperation.CALLERS:
                    for target_id in targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.FIND_CALLERS,
                            operation=operation,
                            target_id=target_id,
                        )
                elif operation == GraphOperation.CALLEES:
                    if selected_site_id:
                        selected_site = self.tools.sites.get(selected_site_id)
                        if selected_site is not None:
                            invoke(
                                Route.GRAPH,
                                ToolName.FIND_CALLEES,
                                operation=operation,
                                site_id=selected_site_id,
                            )
                    else:
                        for target_id in targets:
                            sites = invoke(
                                Route.GRAPH,
                                ToolName.FIND_CALL_SITES,
                                operation=operation,
                                entity_id=target_id,
                            )
                            for site in sites.results if sites else ():
                                site_id = str(site.get("site_id") or "")
                                if site_id:
                                    invoke(
                                        Route.GRAPH,
                                        ToolName.FIND_CALLEES,
                                        operation=operation,
                                        site_id=site_id,
                                    )
                elif operation == GraphOperation.REFERENCES:
                    for target_id in targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.FIND_REFERENCES,
                            operation=operation,
                            target_id=target_id,
                        )
                elif operation == GraphOperation.IMPORTERS:
                    for target_id in targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.FIND_IMPORTERS,
                            operation=operation,
                            target_id=target_id,
                        )
                elif operation == GraphOperation.INHERITANCE:
                    if targets:
                        for target_id in targets:
                            invoke(
                                Route.GRAPH,
                                ToolName.FIND_INHERITANCE,
                                operation=operation,
                                entity_id=target_id,
                            )
                            invoke(
                                Route.GRAPH,
                                ToolName.FIND_OVERRIDES,
                                operation=operation,
                                entity_id=target_id,
                            )
                    else:
                        invoke(Route.GRAPH, ToolName.FIND_INHERITANCE, operation=operation)
                elif operation == GraphOperation.DISPATCH:
                    if selected_site_id:
                        if selected_site_id in self.tools.sites:
                            invoke(
                                Route.GRAPH,
                                ToolName.FIND_DISPATCH_CANDIDATES,
                                operation=operation,
                                site_id=selected_site_id,
                                mode="CONSERVATIVE_DISPATCH",
                            )
                    else:
                        for target_id in targets:
                            sites = invoke(
                                Route.GRAPH,
                                ToolName.FIND_CALL_SITES,
                                operation=operation,
                                entity_id=target_id,
                            )
                            for site in sites.results if sites else ():
                                site_id = str(site.get("site_id") or "")
                                if site_id:
                                    invoke(
                                        Route.GRAPH,
                                        ToolName.FIND_DISPATCH_CANDIDATES,
                                        operation=operation,
                                        site_id=site_id,
                                        mode="CONSERVATIVE_DISPATCH",
                                    )
                elif operation == GraphOperation.IMPACT:
                    for target_id in targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.EXPAND_NEIGHBORHOOD,
                            operation=operation,
                            entity_id=target_id,
                            hop_limit=plan.hop_limit,
                            mode="CONSERVATIVE_DISPATCH",
                        )
                elif operation == GraphOperation.PATHS:
                    if plan.required_waypoints:
                        waypoint_names = {name.casefold() for name in plan.required_waypoints}
                        endpoints = [
                            mention.text
                            for mention in plan.entity_mentions
                            if mention.text.casefold() not in waypoint_names
                        ]
                        path_names = endpoints[:2]
                        required_names = [*path_names, *plan.required_waypoints]
                        missing = [
                            name
                            for name in required_names
                            if name.casefold() not in resolved_mentions
                        ]
                        if len(path_names) < 2 or missing:
                            diagnostics.append(
                                "Required path endpoint or waypoint was not uniquely resolved: "
                                + ", ".join(missing or ["endpoint"])
                            )
                            continue
                        source_id = resolved_mentions[path_names[0].casefold()]
                        destination_id = resolved_mentions[path_names[1].casefold()]
                        waypoint_ids = [
                            resolved_mentions[name.casefold()] for name in plan.required_waypoints
                        ]
                        result = invoke(
                            Route.GRAPH,
                            ToolName.FIND_PATHS,
                            operation=operation,
                            source_id=source_id,
                            destination_id=destination_id,
                            hop_limit=plan.hop_limit,
                            mode=plan.traversal_mode,
                        )
                        if result is not None:
                            matching = []
                            for path in result.results:
                                node_ids = path.get("node_ids")
                                if not isinstance(node_ids, list):
                                    continue
                                position = 0
                                for waypoint_id in waypoint_ids:
                                    position = next(
                                        (
                                            index
                                            for index in range(position + 1, len(node_ids) - 1)
                                            if node_ids[index] == waypoint_id
                                        ),
                                        -1,
                                    )
                                    if position < 0:
                                        break
                                if position >= 0:
                                    matching.append(path)
                            executions[-1] = executions[-1].model_copy(
                                update={
                                    "result": result.model_copy(update={"results": tuple(matching)})
                                }
                            )
                            if not matching:
                                diagnostics.append(
                                    "No graph path satisfies every required waypoint."
                                )
                    elif len(plan.entity_mentions) >= 2 and len(targets) < 2:
                        diagnostics.append("A path endpoint was not uniquely resolved.")
                    elif len(targets) >= 2:
                        for destination_id in targets[1:8]:
                            invoke(
                                Route.GRAPH,
                                ToolName.FIND_PATHS,
                                operation=operation,
                                source_id=targets[0],
                                destination_id=destination_id,
                                hop_limit=plan.hop_limit,
                                mode=plan.traversal_mode,
                            )
                    elif targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.EXPAND_NEIGHBORHOOD,
                            operation=operation,
                            entity_id=targets[0],
                            hop_limit=plan.hop_limit,
                            mode=plan.traversal_mode,
                        )
                elif operation == GraphOperation.NEIGHBORHOOD:
                    for target_id in targets:
                        invoke(
                            Route.GRAPH,
                            ToolName.EXPAND_NEIGHBORHOOD,
                            operation=operation,
                            entity_id=target_id,
                            hop_limit=plan.hop_limit,
                        )

        if Route.FULL_TEXT in plan.routes:
            invoke(Route.FULL_TEXT, ToolName.SEARCH_CODE_TEXT, query=question)
        if Route.VECTOR in plan.routes:
            invoke(Route.VECTOR, ToolName.SEARCH_SEMANTIC_CHUNKS, query=question)
        if Route.GIT in plan.routes:
            if plan.git and plan.git.include_history:
                invoke(Route.GIT, ToolName.GET_GIT_HISTORY)
            else:
                invoke(
                    Route.GIT,
                    ToolName.FIND_CHANGED_PATHS,
                    commit_id=plan.git.commit_id if plan.git else None,
                )
        if Route.RUNTIME in plan.routes:
            invoke(Route.RUNTIME, ToolName.FIND_OBSERVED_TESTS, entity_ids=tuple(targets))
            if GraphOperation.IMPACT in plan.graph_operations and targets:
                invoke(Route.RUNTIME, ToolName.BUILD_TEST_PLAN, entity_ids=tuple(targets))
        if Route.DEPENDENCY in plan.routes:
            records = self.tools.analysis.dependency_metadata.model_dump(mode="json")
            evidence = normalize_graph_evidence(
                evidence_type=EvidenceType.DEPENDENCY_METADATA,
                snapshot_id=snapshot_id,
                source_record_id=stable_id(
                    snapshot_id, "dependency-metadata", prefix="dependency_"
                ),
                display_label="Snapshot dependency metadata",
                provenance_pointer=f"{snapshot_id}:dependency-metadata",
            ).model_dump(mode="json")
            dependency_result = GraphToolResult(
                snapshot_id=snapshot_id,
                tool="dependency_metadata",
                results=({"metadata": records, "evidence": evidence},),
                result_limit=self.tool_limit,
                candidate_count_before_limit=1,
                ordering=("component_id", "requirement_id"),
            )
            executions.append(
                QueryToolExecution(
                    route=Route.DEPENDENCY,
                    request=None,
                    result=dependency_result,
                )
            )

        evidence_pool, evidence_diagnostics = _build_evidence_pool(snapshot_id, executions)
        diagnostics.extend(evidence_diagnostics)
        if len(evidence_pool.items) > MAX_QUERY_EVIDENCE_ITEMS:
            evidence_pool = evidence_pool.model_copy(
                update={"items": evidence_pool.items[:MAX_QUERY_EVIDENCE_ITEMS]}
            )
            truncated = True
            diagnostics.append("Evidence pool reached its deterministic result bound.")
        return QueryResponse(
            snapshot_id=snapshot_id,
            plan=plan,
            provider_id=analysis.provider_id,
            model_id=analysis.model_id,
            estimated_cost_usd=analysis.estimated_cost_usd,
            actual_cost_usd=analysis.actual_cost_usd,
            input_tokens=analysis.input_tokens,
            cached_input_tokens=analysis.cached_input_tokens,
            output_tokens=analysis.output_tokens,
            latency_ms=analysis.latency_ms,
            cache_hit=analysis.cache_hit,
            tool_executions=tuple(executions),
            evidence_pool=evidence_pool,
            truncated=truncated,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )


def _build_evidence_pool(
    snapshot_id: str, executions: Sequence[QueryToolExecution]
) -> tuple[QueryEvidencePool, list[str]]:
    groups: dict[str, list[tuple[EvidenceItem, set[EvidenceOccurrence], str]]] = {}
    diagnostics: list[str] = []

    def visit(
        value: Any,
        execution: QueryToolExecution,
        result_index: int,
        inherited_uncertain: bool,
        inherited_verified_path: bool | None = None,
    ) -> None:
        if isinstance(value, Mapping):
            if {"evidence_id", "type", "snapshot_id", "source_record_id"}.issubset(value):
                try:
                    item = EvidenceItem.model_validate(value)
                    if item.snapshot_id != snapshot_id:
                        diagnostics.append("Rejected cross-snapshot evidence from a tool result.")
                        return
                    occurrence = EvidenceOccurrence(
                        originating_tool=execution.result.tool,
                        result_index=result_index,
                        uncertain=inherited_uncertain,
                        verified_graph_path=value.get("verified_graph_path")
                        if isinstance(value.get("verified_graph_path"), bool)
                        else inherited_verified_path,
                    )
                    groups.setdefault(item.evidence_id, []).append(
                        (item, {occurrence}, item.model_dump_json())
                    )
                except Exception:
                    diagnostics.append("Rejected malformed evidence returned by a graph tool.")
                    return
            for key, child in value.items():
                if key == "evidence":
                    visit(
                        child,
                        execution,
                        result_index,
                        inherited_uncertain,
                        value.get("verified_graph_path")
                        if isinstance(value.get("verified_graph_path"), bool)
                        else inherited_verified_path,
                    )
                elif key in {"steps", "evidence"} or isinstance(child, (dict, list, tuple)):
                    uncertain = (
                        inherited_uncertain
                        or bool(value.get("uncertain"))
                        or bool(value.get("uncertainty_count", 0))
                    )
                    verified = value.get("verified_graph_path")
                    visit(
                        child,
                        execution,
                        result_index,
                        uncertain,
                        verified if isinstance(verified, bool) else inherited_verified_path,
                    )
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(
                    child,
                    execution,
                    result_index,
                    inherited_uncertain,
                    inherited_verified_path,
                )

    for execution in executions:
        for index, result in enumerate(execution.result.results):
            visit(
                result,
                execution,
                index,
                bool(result.get("uncertain")) or bool(result.get("uncertainty_count", 0)),
                result.get("verified_graph_path")
                if isinstance(result.get("verified_graph_path"), bool)
                else None,
            )

    items: list[OrchestratedEvidence] = []
    conflicting_ids: list[str] = []
    for evidence_id in sorted(groups):
        distinct: dict[str, tuple[EvidenceItem, set[EvidenceOccurrence]]] = {}
        for item, occurrences, payload in groups[evidence_id]:
            if payload in distinct:
                distinct[payload][1].update(occurrences)
            else:
                distinct[payload] = (item, set(occurrences))
        conflict = len(distinct) > 1
        if conflict:
            conflicting_ids.append(evidence_id)
            diagnostics.append(f"Conflicting records share evidence ID {evidence_id}.")
        for payload in sorted(distinct):
            item, occurrences = distinct[payload]
            items.append(
                OrchestratedEvidence(
                    item=item,
                    occurrences=tuple(
                        sorted(
                            occurrences,
                            key=lambda occurrence: (
                                occurrence.originating_tool,
                                occurrence.result_index,
                                occurrence.uncertain,
                                occurrence.verified_graph_path is not True,
                            ),
                        )
                    ),
                    conflicting_identity=conflict,
                )
            )
    return (
        QueryEvidencePool(
            snapshot_id=snapshot_id,
            items=tuple(items),
            conflicting_evidence_ids=tuple(conflicting_ids),
        ),
        diagnostics,
    )

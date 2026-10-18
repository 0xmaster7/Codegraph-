from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Route(StrEnum):
    EXACT = "EXACT"
    FULL_TEXT = "FULL_TEXT"
    VECTOR = "VECTOR"
    GRAPH = "GRAPH"
    GIT = "GIT"
    RUNTIME = "RUNTIME"
    DEPENDENCY = "DEPENDENCY"


class GraphOperation(StrEnum):
    CALLERS = "CALLERS"
    CALLEES = "CALLEES"
    REFERENCES = "REFERENCES"
    IMPORTERS = "IMPORTERS"
    INHERITANCE = "INHERITANCE"
    DISPATCH = "DISPATCH"
    IMPACT = "IMPACT"
    PATHS = "PATHS"
    NEIGHBORHOOD = "NEIGHBORHOOD"


class EntityMention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=160)
    kind_hint: Literal["module", "class", "function", "method", "symbol"] | None = None


class GitQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: str | None = Field(
        default=None, min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]+$"
    )
    include_history: bool = False


class QueryPlanProposal(BaseModel):
    """Untrusted, bounded proposal returned by an optional query-analysis provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_mentions: list[EntityMention] = Field(default_factory=list, max_length=32)
    routes: list[Route] = Field(default_factory=list, max_length=len(Route))
    graph_operations: list[GraphOperation] = Field(
        default_factory=list, max_length=len(GraphOperation)
    )
    hop_limit: int = Field(default=3, ge=1, le=5)
    git: GitQuery | None = None
    runtime_required: bool = False


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    entity_mentions: list[EntityMention] = Field(default_factory=list, max_length=32)
    required_waypoints: list[str] = Field(default_factory=list, max_length=32)
    routes: list[Route] = Field(default_factory=list, max_length=len(Route))
    graph_operations: list[GraphOperation] = Field(
        default_factory=list, max_length=len(GraphOperation)
    )
    hop_limit: int = Field(default=3, ge=1, le=5)
    traversal_mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"] = (
        "STRICT_STATIC_BINDING"
    )
    git: GitQuery | None = None
    runtime_required: bool = False
    selected_node_id: str | None = Field(default=None, max_length=160)
    selected_edge_id: str | None = Field(default=None, max_length=160)
    selected_site_id: str | None = Field(default=None, max_length=160)


CODE_TOKENS = re.compile(
    r"`([^`]+)`|\b(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*\b|"
    r"\b[A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\b"
)
CONTEXTUAL_NAME = re.compile(
    r"\b(?:of|for|named|symbol|function|class|method)\s+([A-Za-z_]\w*)\b", re.IGNORECASE
)
HOPS = re.compile(r"\b(?:within|up to|for)\s+(\d+)\s+hops?\b", re.IGNORECASE)
WAYPOINT = re.compile(
    r"\b(?:through|via)\s+(?:`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`|"
    r"((?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*|[A-Z]\w+))"
)
COMMIT_ID = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


def merge_query_plan(
    deterministic: QueryPlan,
    proposal: QueryPlanProposal,
    *,
    question: str,
    snapshot_id: str | None = None,
) -> QueryPlan:
    """Union bounded model suggestions with mandatory deterministic routes."""
    route_set = set(deterministic.routes) | set(proposal.routes)
    operation_set = set(deterministic.graph_operations) | set(proposal.graph_operations)
    if deterministic.entity_mentions or proposal.entity_mentions:
        route_set.add(Route.EXACT)
    if operation_set:
        route_set.add(Route.GRAPH)
    if deterministic.git or proposal.git:
        route_set.add(Route.GIT)
    if deterministic.runtime_required or proposal.runtime_required:
        route_set.add(Route.RUNTIME)

    mentions_by_key: dict[str, EntityMention] = {}
    for mention in (*deterministic.entity_mentions, *proposal.entity_mentions):
        key = mention.text.casefold()
        if key not in mentions_by_key:
            mentions_by_key[key] = mention

    # A model may narrow a default but cannot increase a deterministic user bound.
    hop_limit = deterministic.hop_limit
    if not HOPS.search(question):
        hop_limit = min(deterministic.hop_limit, proposal.hop_limit)

    return QueryPlan(
        snapshot_id=snapshot_id or deterministic.snapshot_id,
        entity_mentions=list(mentions_by_key.values()),
        required_waypoints=deterministic.required_waypoints,
        routes=sorted(route_set, key=lambda item: item.value),
        graph_operations=sorted(operation_set, key=lambda item: item.value),
        hop_limit=hop_limit,
        traversal_mode=deterministic.traversal_mode,
        git=deterministic.git or proposal.git,
        runtime_required=(
            deterministic.runtime_required
            or proposal.runtime_required
            or Route.RUNTIME in route_set
        ),
        selected_node_id=deterministic.selected_node_id,
        selected_edge_id=deterministic.selected_edge_id,
        selected_site_id=deterministic.selected_site_id,
    )


def analyze_query(
    question: str,
    *,
    snapshot_id: str | None = None,
    selected_node_id: str | None = None,
    selected_edge_id: str | None = None,
    selected_site_id: str | None = None,
) -> QueryPlan:
    """Create a safe plan from deterministic query terms; no model or tool executes here."""
    if not question.strip():
        raise ValueError("Question cannot be empty")
    if len(question) > 4096:
        raise ValueError("Question exceeds the 4096-character limit")
    text = question.casefold()

    def contains(*terms: str) -> bool:
        return (
            re.search(r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b", text)
            is not None
        )

    routes: set[Route] = set()
    operations: set[GraphOperation] = set()
    mention_values: dict[str, int] = {}
    for match in CODE_TOKENS.finditer(question):
        value = match.group(1) or match.group(0)
        mention_values[value] = match.start()
    for match in CONTEXTUAL_NAME.finditer(question):
        value = match.group(1)
        mention_values.setdefault(value, match.start(1))
    mentions = [
        EntityMention(text=value)
        for value in sorted(mention_values, key=lambda item: mention_values[item])
    ]

    if mentions:
        routes.add(Route.EXACT)
    if selected_node_id:
        routes.add(Route.EXACT)
    if selected_edge_id or selected_site_id:
        routes.add(Route.GRAPH)
    caller_intent = (
        contains("callers")
        or re.search(
            r"\b(?:who|what)\s+calls\b|\bwho\s+called\b|\b(?:which|what)\s+(?:functions|methods|symbols)\s+call\b|\bcaller\s+relationships?\b",
            text,
        )
        is not None
    )
    callee_intent = (
        contains("callees")
        or re.search(
            r"\bwhat\s+does\b.{0,80}\bcall\b|\bcall\s+sites?\b|\bcalls?\s+made\s+by\b",
            text,
        )
        is not None
    )
    if (
        caller_intent
        or callee_intent
        or contains("caller", "callee", "call", "calls", "call-site", "call-sites")
    ):
        routes.add(Route.GRAPH)
        if caller_intent:
            operations.add(GraphOperation.CALLERS)
        if callee_intent or (not caller_intent and contains("call", "calls")):
            operations.add(GraphOperation.CALLEES)
    if contains("reference", "references", "referenced"):
        routes.add(Route.GRAPH)
        operations.add(GraphOperation.REFERENCES)
    if contains("importer", "importers", "imports", "import"):
        routes.add(Route.GRAPH)
        operations.add(GraphOperation.IMPORTERS)
    if contains(
        "inherit", "inherits", "inheritance", "subclass", "subclasses", "override", "overrides"
    ):
        routes.add(Route.GRAPH)
        operations.add(GraphOperation.INHERITANCE)
    if contains("dispatch", "polymorphic"):
        routes.add(Route.GRAPH)
        operations.add(GraphOperation.DISPATCH)
    if contains(
        "impact",
        "affected",
        "affect",
        "reach",
        "reachable",
        "path",
        "paths",
        "depends",
        "dependency",
    ):
        routes.add(Route.GRAPH)
        operations.add(
            GraphOperation.IMPACT
            if "impact" in text or "affected" in text
            else GraphOperation.PATHS
        )
    required_waypoints: list[str] = []
    if GraphOperation.PATHS in operations:
        for match in WAYPOINT.finditer(question):
            waypoint = match.group(1) or match.group(2)
            assert waypoint is not None
            if waypoint not in required_waypoints:
                required_waypoints.append(waypoint)
                mention_values.setdefault(waypoint, match.start(1 if match.group(1) else 2))
        mentions = [
            EntityMention(text=value)
            for value in sorted(mention_values, key=lambda item: mention_values[item])
        ]
        if required_waypoints:
            routes.add(Route.EXACT)
    if contains("changed", "change", "commit", "diff", "history", "renamed", "rename"):
        routes.add(Route.GIT)
    if contains("test", "tests", "executed", "runtime", "observed"):
        routes.add(Route.RUNTIME)
    if contains("dependency", "dependencies", "package", "version", "requires"):
        routes.add(Route.DEPENDENCY)

    # Always allow text retrieval. Vector retrieval remains optional and separately budgeted.
    routes.add(Route.FULL_TEXT)
    if contains("how", "why", "conceptually", "similar", "explain", "overview") or re.search(
        r"\bwhat\s+is\b", text
    ):
        routes.add(Route.VECTOR)
    if (
        not operations
        and not mentions
        and not routes.intersection({Route.GIT, Route.RUNTIME, Route.DEPENDENCY})
    ):
        routes.add(Route.VECTOR)

    hop_match = HOPS.search(question)
    hop_limit = min(5, max(1, int(hop_match.group(1)))) if hop_match else 3
    traversal_mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"] = (
        "CONSERVATIVE_DISPATCH"
        if contains("may", "could", "possible", "possibly", "dispatch", "polymorphic")
        else "STRICT_STATIC_BINDING"
    )
    commit_match = COMMIT_ID.search(question) if Route.GIT in routes else None
    return QueryPlan(
        snapshot_id=snapshot_id,
        entity_mentions=mentions,
        required_waypoints=required_waypoints,
        routes=sorted(routes, key=lambda item: item.value),
        graph_operations=sorted(operations, key=lambda item: item.value),
        hop_limit=hop_limit,
        traversal_mode=traversal_mode,
        git=(
            GitQuery(
                commit_id=commit_match.group(0) if commit_match else None,
                include_history=contains("history", "commit", "commits"),
            )
            if Route.GIT in routes
            else None
        ),
        runtime_required=Route.RUNTIME in routes,
        selected_node_id=selected_node_id,
        selected_edge_id=selected_edge_id,
        selected_site_id=selected_site_id,
    )

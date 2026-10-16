from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .graph_tools import (
    MAX_TOOL_LIMIT,
    GraphToolRequest,
    PathToolRequest,
)
from .models import SnapshotStatus
from .retrieval import resolve_symbol as resolve_symbol_service
from .tool_services import SnapshotToolContext, load_snapshot_tool_context

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
SnapshotID = Annotated[str, Field(min_length=1, max_length=128, pattern=_ID_PATTERN)]
RecordID = Annotated[str, Field(min_length=1, max_length=160, pattern=_ID_PATTERN)]
QueryText = Annotated[str, Field(min_length=1, max_length=4096)]
ResultLimit = Annotated[int, Field(ge=1, le=MAX_TOOL_LIMIT)]
HopLimit = Annotated[int, Field(ge=1, le=5)]
RecordIDs = Annotated[list[RecordID], Field(max_length=MAX_TOOL_LIMIT)]
PathMode = Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"]


ToolContextLoader = Callable[[str], SnapshotToolContext]


def _json_model(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def create_mcp_server(
    context_loader: ToolContextLoader = load_snapshot_tool_context,
) -> MCPServer:
    """Create the read-only M23 MCP interface over existing backend services."""
    server = MCPServer(
        name="falkordb-codegraph-readonly",
        version="0.1.0",
        instructions=(
            "Read-only investigation tools. Every request is bound to one READY snapshot. "
            "Results preserve the backend's deterministic evidence and uncertainty semantics."
        ),
    )
    readonly = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    def context(snapshot_id: str) -> SnapshotToolContext:
        loaded = context_loader(snapshot_id)
        if loaded.analysis.snapshot_id != snapshot_id:
            raise ValueError("Tool context snapshot does not match the request")
        if loaded.analysis.status != SnapshotStatus.READY:
            raise ValueError("MCP tools require a READY snapshot")
        return loaded

    @server.tool(annotations=readonly, structured_output=True)
    def resolve_symbol(
        snapshot_id: SnapshotID,
        query: QueryText,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Resolve a symbol in one READY snapshot, preserving ambiguity and truncation."""
        loaded = context(snapshot_id)
        result = resolve_symbol_service(loaded.analysis, query, limit=limit)
        return _json_model(result)

    @server.tool(annotations=readonly, structured_output=True)
    def find_callers(
        snapshot_id: SnapshotID,
        target_id: RecordID,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Find exact static call sites bound to a target in the requested snapshot."""
        loaded = context(snapshot_id)
        result = loaded.tools.find_callers(
            GraphToolRequest(snapshot_id=snapshot_id, limit=limit), target_id
        )
        return _json_model(result)

    @server.tool(annotations=readonly, structured_output=True)
    def find_references(
        snapshot_id: SnapshotID,
        target_id: RecordID,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Find exact semantic references to a target in the requested snapshot."""
        loaded = context(snapshot_id)
        result = loaded.tools.find_references(
            GraphToolRequest(snapshot_id=snapshot_id, limit=limit), target_id
        )
        return _json_model(result)

    @server.tool(annotations=readonly, structured_output=True)
    def find_paths(
        snapshot_id: SnapshotID,
        source_id: RecordID,
        target_id: RecordID,
        mode: PathMode = "STRICT_STATIC_BINDING",
        hop_limit: HopLimit = 3,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Find bounded deterministic evidence paths with explicit traversal semantics."""
        loaded = context(snapshot_id)
        request = PathToolRequest(
            snapshot_id=snapshot_id,
            source_id=source_id,
            target_id=target_id,
            mode=mode,
            hop_limit=hop_limit,
            limit=limit,
        )
        return _json_model(loaded.tools.find_paths(request))

    @server.tool(annotations=readonly, structured_output=True)
    def find_changed_symbols(
        snapshot_id: SnapshotID,
        commit_id: RecordID | None = None,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Map recorded changed paths to current symbols as non-exact candidates."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(loaded.tools.find_changed_symbols(request, commit_id=commit_id))

    @server.tool(annotations=readonly, structured_output=True)
    def find_observed_tests(
        snapshot_id: SnapshotID,
        entity_ids: RecordIDs | None = None,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Find runtime observations scoped to this snapshot and its run provenance."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(loaded.tools.find_observed_tests(request, entity_ids=entity_ids or ()))

    @server.tool(annotations=readonly, structured_output=True)
    def get_edge_provenance(
        snapshot_id: SnapshotID,
        edge_id: RecordID,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Return provenance records for one edge in the requested snapshot."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(loaded.tools.get_edge_provenance(request, edge_id))

    @server.tool(annotations=readonly, structured_output=True)
    def search_code_text(
        snapshot_id: SnapshotID,
        query: QueryText,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Search lexical code passages without treating them as graph relationships."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(loaded.tools.search_code_text(request, query.strip()))

    @server.tool(annotations=readonly, structured_output=True)
    def search_semantic_chunks(
        snapshot_id: SnapshotID,
        query: QueryText,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Search semantic passages while retaining passage-only evidence modality."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(loaded.tools.search_semantic_chunks(request, query.strip()))

    @server.tool(annotations=readonly, structured_output=True)
    def build_test_plan(
        snapshot_id: SnapshotID,
        changed_symbol_ids: RecordIDs | None = None,
        limit: ResultLimit = 100,
    ) -> dict[str, Any]:
        """Propose evidence-backed tests; this tool never collects or executes tests."""
        loaded = context(snapshot_id)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return _json_model(
            loaded.tools.build_test_plan(request, changed_symbol_ids=changed_symbol_ids or ())
        )

    return server


def main() -> None:
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()

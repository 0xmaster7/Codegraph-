# Read-only MCP interface

MCP is an adapter over the deterministic backend services. It does not implement
graph traversal, retrieval, or test planning independently, and it exposes no
mutation, shell, Cypher, or test-execution action.

Run the stdio server from the repository root after starting the configured
FalkorDB service and completing ingestion:

```sh
uv run python -m backend.app.mcp_server
```

Every tool request names a snapshot. The adapter rejects malformed identifiers,
out-of-range result limits and hop counts, snapshots that are not `READY`, and
analysis records whose snapshot identity differs from the request. Read-only
tool annotations are included in the MCP tool list.

The V1 tools are `resolve_symbol`, `find_callers`, `find_references`,
`find_paths`, `find_changed_symbols`, `find_observed_tests`,
`get_edge_provenance`, `search_code_text`, `search_semantic_chunks`, and
`build_test_plan`. `build_test_plan` only recommends tests; it does not collect
or execute them. Runtime execution remains on the separately approved UI/API
path.

`backend.app.tool_services.load_snapshot_tool_context` binds requests to one
READY snapshot. MCP delegates to the existing symbol resolution and
`DeterministicGraphTools` methods. REST handlers for the overlapping endpoints
use the same service methods. The parity test compares direct service values,
FastAPI JSON encoding, and MCP structured content after normalization.

## M23 coverage

| Requirement | Implementation / verification |
|---|---|
| Ten specified read-only tools | `backend/app/mcp_server.py`; tool-list test checks exact names |
| Shared backend behavior | Existing resolver and `DeterministicGraphTools` service calls; normalization parity test |
| READY snapshot binding | `load_snapshot_tool_context`; mismatch and failed-snapshot rejection tests |
| Bounded inputs | Pydantic schemas cap IDs, queries, result limits (1–500), and path hops (1–5); malformed-input tests |
| No execution action | Tool-list test confirms no `run_approved_tests`; test plan tool is advisory only |

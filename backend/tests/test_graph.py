from __future__ import annotations

from typing import Any

import pytest

from backend.app.embeddings import EmbeddedVector, EmbeddingMeasurement, EmbeddingSpec
from backend.app.graph import GraphStore


class Result:
    def __init__(self, header: list[list[Any]], rows: list[list[Any]]):
        self.header = header
        self.result_set = rows


class FakeGraph:
    def __init__(self, kind: str):
        self.kind = kind
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def query(self, query: str, params: dict[str, Any] | None = None) -> Result:
        self.queries.append((query, params or {}))
        if self.kind == "metadata":
            return Result(
                [["s.graph_name", "STRING"], ["s.status", "STRING"]],
                [["code_fixture", "READY"]],
            )
        if "count(edge)" in query:
            return Result([["count(edge)", "INTEGER"]], [[2]])
        if "WHERE n.qualified_name = $query" in query:
            rows = (
                [["sym_1", "Function", "verify", "auth.verify", "auth.py", 0, 30]]
                if (params or {}).get("query") == "auth.verify"
                else []
            )
            return Result(
                [
                    ["id", "STRING"],
                    ["kind", "STRING"],
                    ["name", "STRING"],
                    ["qualified_name", "STRING"],
                    ["file_path", "STRING"],
                    ["start_byte", "INTEGER"],
                    ["end_byte", "INTEGER"],
                ],
                rows,
            )
        if "WHERE n.name = $query" in query:
            if (params or {}).get("query") == "validate":
                return Result(
                    [
                        ["id", "STRING"],
                        ["kind", "STRING"],
                        ["name", "STRING"],
                        ["qualified_name", "STRING"],
                        ["file_path", "STRING"],
                        ["start_byte", "INTEGER"],
                        ["end_byte", "INTEGER"],
                    ],
                    [
                        ["sym_1", "Method", "validate", "a.validate", "a.py", 0, 10],
                        ["sym_2", "Method", "validate", "b.validate", "b.py", 0, 10],
                    ],
                )
            return Result([], [])
        if "WHERE n.id = $query" in query:
            return Result([], [])
        if "identifier_search_text = $normalized" in query:
            return Result([], [])
        if "db.idx.fulltext.queryNodes" in query:
            return Result([], [])
        if "target.qualified_name AS qualified_name" in query:
            return Result(
                [
                    ["target_id", "STRING"],
                    ["target_labels", "ARRAY"],
                    ["name", "STRING"],
                    ["qualified_name", "STRING"],
                    ["file_path", "STRING"],
                    ["edge_id", "STRING"],
                    ["binding_state", "STRING"],
                ],
                [
                    [
                        "sym_2",
                        ["SourceEntity"],
                        "verify",
                        "auth.verify",
                        "auth.py",
                        "edge_2",
                        "EXACT",
                    ]
                ],
            )
        return Result(
            [
                ["site_id", "STRING"],
                ["kind", "STRING"],
                ["expression", "STRING"],
                ["file_path", "STRING"],
                ["start_byte", "INTEGER"],
                ["end_byte", "INTEGER"],
                ["edge_id", "STRING"],
                ["binding_state", "STRING"],
            ],
            [["site_1", "CALL_CALLEE", "verify", "test.py", 10, 16, "edge_1", "EXACT"]],
        )


class EmbeddingGraph:
    def __init__(self, stored_hash: str):
        self.stored_hash = stored_hash
        self.queries: list[str] = []

    def query(self, query: str, _params: dict[str, Any] | None = None) -> Result:
        self.queries.append(query)
        if "RETURN c.content_hash" in query:
            return Result([], [[self.stored_hash, None]])
        if query == "CALL db.indexes()":
            return Result([], [])
        return Result([], [])


@pytest.mark.parametrize("actual_hash", ["wrong", ""])
def test_embedding_writer_rejects_content_hash_mismatch_before_mutation(
    monkeypatch: pytest.MonkeyPatch, actual_hash: str
) -> None:
    store = GraphStore.__new__(GraphStore)
    spec = EmbeddingSpec(provider="test", model="test-v1", dimensions=2)
    vector = EmbeddedVector(
        chunk_id="chunk-a",
        snapshot_id="snap-a",
        spec_id=spec.identity,
        content_hash="expected",
        vector=(0.2, 0.8),
    )
    graph = EmbeddingGraph(actual_hash)
    monkeypatch.setattr(store, "snapshot_status", lambda _snapshot: "READY")
    monkeypatch.setattr(store, "snapshot_graph", lambda _snapshot: (graph, "graph-a"))
    monkeypatch.setattr(store, "_analysis_chunks", lambda _snapshot: {"chunk-a": "CodeChunk"})

    with pytest.raises(ValueError, match="content hash"):
        store.store_chunk_embeddings("snap-a", EmbeddingMeasurement(spec, [vector], 0, 0.0))

    assert not any("SET c.embedding_" in query for query in graph.queries)


class FakeClient:
    def __init__(self):
        self.meta = FakeGraph("metadata")
        self.code = FakeGraph("code")

    def select_graph(self, name: str) -> FakeGraph:
        return self.meta if name == "codegraph_metadata" else self.code


def store() -> tuple[GraphStore, FakeClient]:
    client = FakeClient()
    graph_store = GraphStore.__new__(GraphStore)
    graph_store.client = client
    return graph_store, client


def test_exact_graph_resolution_returns_first_exact_tier_and_status() -> None:
    graph_store, client = store()
    result = graph_store.resolve_exact("snap_1", "auth.verify")
    assert result["matched_by"] == "QUALIFIED_NAME"
    assert result["snapshot_status"] == "READY"
    assert result["results"][0]["id"] == "sym_1"
    assert len(client.code.queries) == 2


def test_graph_resolution_reports_explicit_not_found_after_all_retrieval_tiers() -> None:
    graph_store, _ = store()
    result = graph_store.resolve_exact("snap_1", "missing_symbol")
    assert result["status"] == "NOT_FOUND"
    assert result["matched_by"] is None
    assert result["results"] == []


def test_graph_resolution_keeps_same_name_symbols_as_disambiguation_set() -> None:
    graph_store, _ = store()
    result = graph_store.resolve_exact("snap_1", "validate")
    assert result["status"] == "AMBIGUOUS"
    assert result["matched_by"] == "NAME"
    assert [item["qualified_name"] for item in result["results"]] == [
        "a.validate",
        "b.validate",
    ]

    bounded = graph_store.resolve_exact("snap_1", "validate", limit=1)
    assert bounded["status"] == "AMBIGUOUS"
    assert bounded["truncated"] is True
    assert len(bounded["results"]) == 1


def test_fulltext_index_creation_is_idempotent_for_actual_catalog_shape() -> None:
    class IndexCatalog:
        def __init__(self) -> None:
            self.created: list[str] = []

        def query(self, query: str) -> Result:
            if query == "CALL db.indexes()":
                return Result(
                    [],
                    [
                        [
                            "SourceEntity",
                            ["identifier_raw", "identifier_search_text"],
                            {
                                "identifier_raw": ["FULLTEXT"],
                                "identifier_search_text": ["FULLTEXT"],
                            },
                        ]
                    ],
                )
            self.created.append(query)
            return Result([], [])

    graph = IndexCatalog()
    GraphStore._ensure_symbol_fulltext_index(graph)  # type: ignore[arg-type]
    assert graph.created == []


def test_records_accepts_live_falkordb_typed_column_headers() -> None:
    result = Result([[1, "symbol_id"], [1, "name"]], [["sym_1", "verify"]])
    assert GraphStore._records(result) == [{"symbol_id": "sym_1", "name": "verify"}]


def test_index_bootstrap_recognizes_all_properties_in_falkordb_index_rows() -> None:
    class IndexCatalog:
        def __init__(self) -> None:
            self.created: list[str] = []

        def query(self, query: str) -> Result:
            if query == "CALL db.indexes()":
                return Result(
                    [],
                    [["RepositorySnapshot", ["snapshot_id", "repository_id", "status"]]],
                )
            self.created.append(query)
            return Result([], [])

    graph = IndexCatalog()
    GraphStore._ensure_indexes(
        graph,  # type: ignore[arg-type]
        [
            ("RepositorySnapshot", "snapshot_id"),
            ("RepositorySnapshot", "repository_id"),
            ("RepositorySnapshot", "status"),
            ("RepositorySnapshot", "ready_at"),
        ],
    )

    assert graph.created == ["CREATE INDEX FOR (n:RepositorySnapshot) ON (n.ready_at)"]


def test_graph_callers_are_bounded_ordered_and_surface_snapshot_state() -> None:
    graph_store, client = store()
    result = graph_store.find_callers("snap_1", "sym_1", limit=1)
    assert result["mode"] == "STRICT_STATIC_BINDING"
    assert result["candidate_count_before_limit"] == 2
    assert result["truncated"] is True
    assert result["results"][0]["site_id"] == "site_1"
    assert not result["limitations"]
    assert "ORDER BY site.file_path, site.start_byte, site.id" in client.code.queries[-1][0]


def test_graph_binding_queries_exclude_authoritatively_failed_edges() -> None:
    graph_store, client = store()
    callers = graph_store.find_callers("snap_1", "sym_1", excluded_edge_ids=("edge-failed",))

    assert "Relationships with replay integrity failures were excluded." in callers["limitations"]
    for query, params in client.code.queries:
        assert "NOT edge.edge_id IN $excluded_edge_ids" in query
        assert params["excluded_edge_ids"] == ["edge-failed"]


def test_graph_references_and_callees_are_exact_bounded_queries() -> None:
    graph_store, client = store()
    references = graph_store.find_references("snap_1", "sym_1", limit=1)
    callees = graph_store.find_callees("snap_1", "site_1", limit=1)

    assert references["mode"] == "EXACT_SEMANTIC_BINDING"
    assert references["truncated"] is True
    assert references["results"][0]["site_id"] == "site_1"
    assert "ReferenceSite" in client.code.queries[1][0]
    assert callees["mode"] == "EXACT_SEMANTIC_BINDING"
    assert callees["truncated"] is True
    assert callees["results"][0]["target_id"] == "sym_2"
    assert "CallSite" in client.code.queries[-1][0]


def test_runtime_tool_records_are_loaded_only_from_the_requested_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeEvidenceGraph:
        def __init__(self) -> None:
            self.queries: list[tuple[str, dict[str, Any]]] = []

        def query(self, query: str, params: dict[str, Any] | None = None) -> Result:
            self.queries.append((query, params or {}))
            if "RuntimeFunctionEntry" in query:
                return Result(
                    [
                        ["entry_id", "STRING"],
                        ["snapshot_id", "STRING"],
                        ["test_definition_id", "STRING"],
                        ["entity_id", "STRING"],
                    ],
                    [["entry-a", "snap-1", "test-a", "entity-a"]],
                )
            return Result(
                [
                    ["observation_id", "STRING"],
                    ["snapshot_id", "STRING"],
                    ["test_definition_id", "STRING"],
                    ["entity_id", "STRING"],
                    ["source_entity_id", "STRING"],
                ],
                [["call-a", "snap-1", "test-a", "target-a", "source-a"]],
            )

    graph = RuntimeEvidenceGraph()
    graph_store = GraphStore.__new__(GraphStore)
    monkeypatch.setattr(graph_store, "snapshot_graph", lambda _snapshot: (graph, "READY"))

    records = graph_store.runtime_tool_records("snap-1")

    assert [item["entry_id"] for item in records if item.get("entry_id")] == ["entry-a"]
    assert records[1]["entity_id"] == "target-a"
    assert all(query_params == {"snapshot_id": "snap-1"} for _, query_params in graph.queries)
    assert all(
        "-[:DURING]->" in query and "-[:INSTANCE_OF]->" in query for query, _ in graph.queries
    )


def test_snapshot_dashboard_lists_latest_repository_metadata() -> None:
    class SnapshotMetadata:
        def __init__(self) -> None:
            self.query_text = ""
            self.params: dict[str, Any] = {}

        def query(self, query: str, params: dict[str, Any] | None = None) -> Result:
            self.query_text = query
            self.params = params or {}
            return Result(
                [
                    ["snapshot_id", "STRING"],
                    ["repository_id", "STRING"],
                    ["repository_root", "STRING"],
                    ["status", "STRING"],
                ],
                [["snap_new", "repo_1", "/workspace/project", "READY"]],
            )

    metadata = SnapshotMetadata()
    graph_store = GraphStore.__new__(GraphStore)
    graph_store.client = type("Client", (), {"select_graph": lambda _self, _name: metadata})()

    snapshots = graph_store.list_snapshots(limit=10)

    assert snapshots == [
        {
            "snapshot_id": "snap_new",
            "repository_id": "repo_1",
            "repository_root": "/workspace/project",
            "status": "READY",
        }
    ]
    assert "coalesce(s.updated_at, s.ready_at, s.created_at, 0) DESC" in metadata.query_text
    assert metadata.params == {"limit": 10}

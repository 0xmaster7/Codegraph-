from __future__ import annotations

import hashlib

import pytest

from backend.app.graph_tools import (
    DeterministicGraphTools,
    GraphToolRequest,
    GraphToolResult,
    ImpactToolRequest,
    PathToolRequest,
)
from backend.app.models import (
    ChangedPath,
    DerivedEdge,
    GitChangeStatus,
    GitCommit,
    GitDeveloper,
    ProvenanceRecord,
    ReplayOutcome,
    ReplayResult,
    ReplayScope,
    RepositoryInfo,
    RetrievalChunk,
    ReviewState,
    SemanticBinding,
    SiteDispatch,
    SiteKind,
    SnapshotAnalysis,
    SnapshotManifestEntry,
    SnapshotStatus,
    SourceEntity,
    SourceSite,
    SourceSpan,
    StructuralImport,
)

SNAPSHOT = "snap-tools"
SOURCE_A_HASH = hashlib.sha256(b"a" * 20).hexdigest()


def _span(start: int = 0, end: int = 20) -> SourceSpan:
    return SourceSpan(
        start_byte=start,
        end_byte=end,
        start_line=1,
        start_column=start,
        end_line=1,
        end_column=end,
    )


def _analysis() -> SnapshotAnalysis:
    entities = [
        SourceEntity(
            entity_id="mod-a",
            kind="Module",
            name="a",
            qualified_name="a",
            file_path="a.py",
            span=_span(),
            parent_id="file-a",
        ),
        SourceEntity(
            entity_id="file-a",
            kind="File",
            name="a.py",
            qualified_name="a.py",
            file_path="a.py",
            span=_span(),
        ),
        SourceEntity(
            entity_id="mod-b",
            kind="Module",
            name="b",
            qualified_name="b",
            file_path="b.py",
            span=_span(),
            parent_id="file-b",
        ),
        SourceEntity(
            entity_id="file-b",
            kind="File",
            name="b.py",
            qualified_name="b.py",
            file_path="b.py",
            span=_span(),
        ),
        SourceEntity(
            entity_id="caller",
            kind="Function",
            name="caller",
            qualified_name="a.caller",
            file_path="a.py",
            span=_span(1, 18),
            parent_id="mod-a",
        ),
        SourceEntity(
            entity_id="callee",
            kind="Function",
            name="callee",
            qualified_name="b.callee",
            file_path="b.py",
            span=_span(1, 18),
            parent_id="mod-b",
        ),
        SourceEntity(
            entity_id="base",
            kind="Class",
            name="Base",
            qualified_name="b.Base",
            file_path="b.py",
            span=_span(1, 8),
            parent_id="mod-b",
        ),
        SourceEntity(
            entity_id="derived",
            kind="Class",
            name="Derived",
            qualified_name="b.Derived",
            file_path="b.py",
            span=_span(9, 18),
            parent_id="mod-b",
        ),
        SourceEntity(
            entity_id="base-run",
            kind="Method",
            name="run",
            qualified_name="b.Base.run",
            file_path="b.py",
            span=_span(2, 7),
            parent_id="base",
        ),
        SourceEntity(
            entity_id="derived-run",
            kind="Method",
            name="run",
            qualified_name="b.Derived.run",
            file_path="b.py",
            span=_span(10, 17),
            parent_id="derived",
        ),
    ]
    sites = [
        SourceSite(
            site_id="call-known",
            kind=SiteKind.CALL_CALLEE,
            expression="callee",
            file_path="a.py",
            span=_span(5, 11),
            context_id="caller",
            syntax_node_type="identifier",
        ),
        SourceSite(
            site_id="call-known-parallel",
            kind=SiteKind.CALL_CALLEE,
            expression="callee",
            file_path="a.py",
            span=_span(10, 16),
            context_id="caller",
            syntax_node_type="identifier",
        ),
        SourceSite(
            site_id="call-unknown",
            kind=SiteKind.CALL_CALLEE,
            expression="mystery",
            file_path="a.py",
            span=_span(12, 19),
            context_id="caller",
            syntax_node_type="identifier",
        ),
        SourceSite(
            site_id="ref-base",
            kind=SiteKind.VALUE_REFERENCE,
            expression="Base",
            file_path="a.py",
            span=_span(2, 6),
            context_id="caller",
            syntax_node_type="identifier",
        ),
        SourceSite(
            site_id="import-b",
            kind=SiteKind.IMPORT_REFERENCE,
            expression="b",
            file_path="a.py",
            span=_span(0, 1),
            context_id="mod-a",
            syntax_node_type="dotted_name",
        ),
    ]
    binding = SemanticBinding(
        binding_id="binding-callee",
        edge_id="edge-bind-callee",
        site_id="call-known",
        target_entity_id="callee",
        scip_symbol="b.callee.",
        occurrence_start_byte=5,
    )
    reference = SemanticBinding(
        binding_id="binding-base",
        edge_id="edge-bind-base",
        site_id="ref-base",
        target_entity_id="base",
        scip_symbol="b.Base#",
        occurrence_start_byte=2,
    )
    parallel_binding = SemanticBinding(
        binding_id="binding-callee-parallel",
        edge_id="edge-bind-callee-parallel",
        site_id="call-known-parallel",
        target_entity_id="callee",
        scip_symbol="b.callee.",
        occurrence_start_byte=10,
    )
    imports = [
        StructuralImport(
            edge_id="edge-import-b",
            site_id="import-b",
            source_module_id="mod-a",
            target_module_id="mod-b",
            imported_module="b",
            resolver="repository-module-index",
            span=_span(0, 1),
        )
    ]
    derived = [
        DerivedEdge(
            edge_id="edge-inherits",
            relation="INHERITS",
            source_id="derived",
            target_id="base",
            rule_id="inheritance.v1",
            premise_edge_ids=["base-site"],
        ),
        DerivedEdge(
            edge_id="edge-overrides",
            relation="OVERRIDES",
            source_id="derived-run",
            target_id="base-run",
            rule_id="overrides.v1",
            premise_edge_ids=["edge-inherits"],
        ),
        DerivedEdge(
            edge_id="edge-dispatch",
            relation="MAY_DISPATCH_TO",
            source_id="call-known",
            target_id="derived-run",
            rule_id="dispatch.v1",
            premise_edge_ids=["edge-bind-callee"],
        ),
    ]
    provenance = [
        ProvenanceRecord(
            provenance_id=f"prov-{edge_id}",
            edge_id=edge_id,
            snapshot_id=SNAPSHOT,
            file_path="a.py",
            file_content_hash=SOURCE_A_HASH,
            span=_span(1, 4),
            syntax_node_type="identifier",
            extractor_version="1",
            rule_id=f"rule.{edge_id}",
        )
        for edge_id in (
            "edge-bind-callee",
            "edge-bind-base",
            "edge-import-b",
            "edge-inherits",
            "edge-overrides",
            "edge-dispatch",
        )
    ]
    return SnapshotAnalysis(
        repository=RepositoryInfo(repository_id="repo-tools", root="/repo", is_dirty=False),
        snapshot_id=SNAPSHOT,
        status=SnapshotStatus.READY,
        manifest=[
            SnapshotManifestEntry(
                path="a.py",
                content_hash=SOURCE_A_HASH,
                blob_hash=SOURCE_A_HASH,
                size_bytes=20,
                analyzed=True,
            ),
            SnapshotManifestEntry(
                path="b.py", content_hash="hash-b", blob_hash="hash-b", size_bytes=20, analyzed=True
            ),
            SnapshotManifestEntry(
                path="docs/guide.md",
                content_hash="hash-doc-file",
                blob_hash="hash-doc-file",
                size_bytes=len(b"document evidence"),
                analyzed=True,
            ),
        ],
        entities=entities,
        retrieval_chunks=[
            RetrievalChunk(
                chunk_id="code-a",
                snapshot_id=SNAPSHOT,
                source_node_id="caller",
                file_path="a.py",
                chunk_kind="FUNCTION_BODY",
                source_start_byte=0,
                source_end_byte=len(b"function body"),
                text="function body",
                content_hash=hashlib.sha256(b"function body").hexdigest(),
                chunk_strategy_version="chunk-v1",
            ),
            RetrievalChunk(
                chunk_id="doc-a",
                snapshot_id=SNAPSHOT,
                source_node_id="doc-source-a",
                file_path="docs/guide.md",
                chunk_kind="DOCUMENT",
                source_start_byte=0,
                source_end_byte=len(b"document evidence"),
                text="document evidence",
                content_hash=hashlib.sha256(b"document evidence").hexdigest(),
                chunk_strategy_version="chunk-v1",
            ),
        ],
        sites=sites,
        provenance=provenance,
        bindings=[binding, reference, parallel_binding],
        imports=imports,
        derived_edges=derived,
        dispatch=[
            SiteDispatch(
                site_id="call-known",
                state="MULTI_CANDIDATE",
                candidate_entity_ids=["callee", "derived-run"],
            )
        ],
        developers=[GitDeveloper(developer_id="dev", name="Dev", email="dev@example.test")],
        commits=[
            GitCommit(
                commit_id="commit-1",
                commit_sha="sha-1",
                author_id="dev",
                committer_id="dev",
                authored_at="2026-01-01T00:00:00Z",
                committed_at="2026-01-01T00:00:00Z",
                message="change source",
            )
        ],
        changed_paths=[
            ChangedPath(
                change_id="change-1",
                commit_id="commit-1",
                parent_commit_sha="parent",
                parent_index=0,
                status=GitChangeStatus.MODIFIED,
                old_path="a.py",
                new_path="a.py",
            ),
            ChangedPath(
                change_id="change-2",
                commit_id="commit-1",
                parent_commit_sha="parent",
                parent_index=0,
                status=GitChangeStatus.MODIFIED,
                old_path="b.py",
                new_path="b.py",
            ),
        ],
        diagnostics=[],
        counts={},
    )


def _tools(**kwargs: object) -> DeterministicGraphTools:
    return DeterministicGraphTools(_analysis(), **kwargs)  # type: ignore[arg-type]


def _request(*, limit: int = 100, snapshot_id: str = SNAPSHOT) -> GraphToolRequest:
    return GraphToolRequest(snapshot_id=snapshot_id, limit=limit)


def test_symbol_resolution_and_source_span_keep_exact_snapshot_provenance() -> None:
    tools = _tools(source_loader=lambda path: b"a" * 20 if path == "a.py" else None)
    exact = tools.resolve_symbol(_request(), "a.caller")
    assert exact.results[0]["id"] == "caller"
    assert exact.snapshot_id == SNAPSHOT
    span = tools.get_source_span(_request(), "a.py", 2, 6)
    assert span.results[0]["file_content_hash"] == SOURCE_A_HASH
    assert span.results[0]["text"] == "aaaa"
    stale = _tools(source_loader=lambda _path: b"changed source bytes")
    rejected = stale.get_source_span(_request(), "a.py", 2, 6)
    assert rejected.results == ()
    assert "hash" in rejected.diagnostics[0]


def test_call_site_tools_preserve_parallel_sites_and_report_unresolved_sites() -> None:
    tools = _tools()
    sites = tools.find_call_sites(_request(), context_id="caller")
    assert [item["site_id"] for item in sites.results] == [
        "call-known",
        "call-known-parallel",
        "call-unknown",
    ]
    assert sites.results[2]["binding_state"] == "UNRESOLVED"
    callers = tools.find_callers(_request(), "callee")
    assert callers.results[0]["caller"]["id"] == "caller"
    callees = tools.find_callees(_request(), "call-known")
    assert {item["target_id"] for item in callees.results} == {"callee"}
    refs = tools.find_references(_request(), "base")
    assert refs.results[0]["resolution"] == "EXACT"
    assert refs.results[0]["provenance_ids"] == ["prov-edge-bind-base"]


def test_relationship_tools_return_only_explicit_structural_evidence() -> None:
    tools = _tools()
    assert tools.find_importers(_request(), "mod-b").results[0]["source_module_id"] == "mod-a"
    assert tools.find_inheritance(_request(), "derived").results[0]["edge_id"] == "edge-inherits"
    assert tools.find_overrides(_request(), "derived-run").results[0]["target_id"] == "base-run"
    dispatch = tools.find_dispatch_candidates(_request(), "call-known")
    assert {item["target_id"] for item in dispatch.results} == {"derived-run"}
    assert all("CALLS" != item.get("relation") for item in dispatch.results)


def test_path_tools_are_bounded_deterministic_and_surface_unresolved_barriers() -> None:
    tools = _tools()
    request = PathToolRequest(
        snapshot_id=SNAPSHOT,
        source_id="caller",
        target_id="callee",
        hop_limit=3,
        limit=1,
    )
    strict = tools.find_paths(request)
    assert strict.mode == "STRICT_STATIC_BINDING"
    assert strict.hop_limit == 3
    assert strict.truncated
    assert strict.results[0]["node_ids"] == ["caller", "callee"]
    assert strict.results[0]["steps"][0]["verified_graph_path"] is True
    path_evidence = strict.results[0]["steps"][0]["evidence"]
    assert {item["type"] for item in path_evidence} == {
        "STATIC_STRUCTURE",
        "STATIC_BINDING",
    }
    assert strict.candidate_count_is_lower_bound is False
    assert [item["site_id"] for item in strict.unresolved_barriers] == ["call-unknown"]

    conservative_request = PathToolRequest(
        snapshot_id=SNAPSHOT,
        source_id="caller",
        target_id="derived-run",
        mode="CONSERVATIVE_DISPATCH",
        hop_limit=3,
        limit=10,
    )
    conservative = tools.find_paths(conservative_request)
    assert conservative.results[0]["target_id"] == "derived-run"
    assert conservative.results[0]["uncertainty_count"] == 1
    assert not tools.find_paths(
        conservative_request.model_copy(update={"mode": "STRICT_STATIC_BINDING"})
    ).results
    shortest = tools.find_shortest_evidence_path(request)
    assert shortest.results[0]["hop_count"] == 1
    neighborhood = tools.expand_neighborhood(_request(), "caller", hop_limit=1)
    assert neighborhood.hop_limit == 1
    assert "callee" in {item["node_id"] for item in neighborhood.results}


def test_impact_traversal_keeps_ambiguous_dispatch_conservative() -> None:
    tools = _tools()
    conservative = tools.find_impact(
        ImpactToolRequest(
            snapshot_id=SNAPSHOT,
            target_id="derived-run",
            mode="CONSERVATIVE_DISPATCH",
            hop_limit=3,
            limit=10,
        )
    )
    assert any(item["node_id"] == "caller" for item in conservative.results)
    caller_path = next(item for item in conservative.results if item["node_id"] == "caller")
    assert caller_path["path"] == ["caller", "derived-run"]
    assert caller_path["uncertainty_count"] == 1
    assert caller_path["steps"][0]["relation"] == "CONSERVATIVE_DISPATCH"
    strict = tools.find_impact(
        ImpactToolRequest(
            snapshot_id=SNAPSHOT,
            target_id="derived-run",
            mode="STRICT_STATIC_BINDING",
            hop_limit=3,
            limit=10,
        )
    )
    assert "caller" not in {item["node_id"] for item in strict.results}


def test_nonexistent_wrong_snapshot_and_ambiguous_lookup_fail_closed() -> None:
    tools = _tools()
    missing = tools.resolve_symbol(_request(), "not-there")
    assert missing.results == ()
    assert missing.diagnostics
    assert tools.find_callers(_request(), "not-there").diagnostics
    assert tools.find_paths(
        PathToolRequest(snapshot_id=SNAPSHOT, source_id="missing", target_id="callee")
    ).diagnostics
    try:
        tools.find_call_sites(_request(snapshot_id="snap-other"))
    except KeyError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("wrong snapshot scope must be rejected")
    ambiguous = tools.resolve_symbol(_request(), "run")
    assert len(ambiguous.results) == 2
    assert {item["resolution_status"] for item in ambiguous.results} == {"AMBIGUOUS"}


def test_provenance_replay_history_and_changed_symbol_tools_are_ordered() -> None:
    tools = _tools(
        replay=lambda edge_id: ReplayResult(
            edge_id=edge_id,
            scope=ReplayScope.FILE_OR_SPAN,
            outcome=ReplayOutcome.PASS,
            review_state=ReviewState.NONE,
            message="Evidence replay passed.",
        )
    )
    provenance = tools.get_edge_provenance(_request(), "edge-bind-callee")
    assert provenance.results[0]["provenance_id"] == "prov-edge-bind-callee"
    assert tools.replay_evidence(_request(), "edge-bind-callee").results[0]["outcome"] == "PASS"
    assert tools.get_git_history(_request()).results[0]["commit_sha"] == "sha-1"
    assert tools.find_changed_paths(_request()).results[0]["change_id"] == "change-1"
    symbols = tools.find_changed_symbols(_request())
    assert {item["id"] for item in symbols.results} >= {"caller", "callee"}
    assert all(item["exact_symbol_diff"] is False for item in symbols.results)


@pytest.mark.parametrize(
    ("outcome", "eligible"),
    [
        (ReplayOutcome.PASS, True),
        (ReplayOutcome.NOT_RUN, True),
        (ReplayOutcome.ENVIRONMENT_MISMATCH, True),
        (ReplayOutcome.EXTRACTOR_VERSION_MISMATCH, True),
        (ReplayOutcome.PREMISE_MISSING, True),
        (ReplayOutcome.ERROR, True),
        (ReplayOutcome.EDGE_NOT_REPRODUCED, False),
        (ReplayOutcome.SOURCE_HASH_MISMATCH, False),
    ],
)
def test_replay_outcome_controls_only_affected_exact_evidence(
    outcome: ReplayOutcome, eligible: bool
) -> None:
    analysis = _analysis()
    provenance = [
        item.model_copy(update={"replay_state": outcome})
        if item.edge_id == "edge-bind-callee"
        else item
        for item in analysis.provenance
    ]
    tools = DeterministicGraphTools(analysis.model_copy(update={"provenance": provenance}))

    call_sites = tools.find_call_sites(_request(), context_id="caller")
    known = next(item for item in call_sites.results if item["site_id"] == "call-known")
    assert known["binding_state"] == ("EXACT" if eligible else "REPLAY_INTEGRITY_FAILURE")

    historical = tools.get_edge_provenance(_request(), "edge-bind-callee")
    assert historical.results[0]["replay_state"] == outcome.value
    evidence = tools._edge_evidence("edge-bind-callee", "BINDS_TO", "call-known", "callee")
    assert evidence["replay_outcome"] == outcome.value
    if outcome == ReplayOutcome.PASS:
        assert evidence["review_state"] == ReviewState.NONE.value
    elif outcome != ReplayOutcome.NOT_RUN:
        assert evidence["review_state"] == ReviewState.REPLAY_FAILED.value

    callers = tools.find_callers(_request(), "callee")
    edges = {record["edge_id"] for record in callers.results}
    assert ("edge-bind-callee" in edges) is eligible
    assert "edge-bind-callee-parallel" in edges
    assert ("edge-bind-callee" in tools.fail_closed_edge_ids) is (not eligible)

    path = tools.find_paths(
        PathToolRequest(
            snapshot_id=SNAPSHOT,
            source_id="caller",
            target_id="callee",
            hop_limit=2,
        )
    )
    path_edges = {
        edge_id
        for candidate in path.results
        for step in candidate["steps"]
        for edge_id in step["edge_ids"]
    }
    assert ("edge-bind-callee" in path_edges) is eligible
    if not eligible:
        assert "integrity" in " ".join(path.diagnostics).lower()
        assert "edge-bind-callee" not in path_edges


def test_replay_state_is_snapshot_scoped() -> None:
    original = _analysis()
    altered = original.model_copy(
        update={
            "snapshot_id": "snap-other-replay",
            "provenance": [
                item.model_copy(
                    update={
                        "snapshot_id": "snap-other-replay",
                        "replay_state": ReplayOutcome.SOURCE_HASH_MISMATCH,
                    }
                )
                if item.edge_id == "edge-bind-callee"
                else item.model_copy(update={"snapshot_id": "snap-other-replay"})
                for item in original.provenance
            ],
        }
    )

    original_tools = DeterministicGraphTools(original)
    other_tools = DeterministicGraphTools(altered)
    assert "edge-bind-callee" not in original_tools.fail_closed_edge_ids
    assert "edge-bind-callee" in other_tools.fail_closed_edge_ids
    assert "edge-bind-callee" in {
        item["edge_id"] for item in original_tools.find_callers(_request(), "callee").results
    }


def test_runtime_test_tools_keep_observations_separate_and_select_only_observed_tests() -> None:
    records = [
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-a",
            "test_invocation_id": "inv-a",
            "entity_id": "caller",
            "run_id": "run-a",
            "entry_id": "entry-a",
        },
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-b",
            "test_invocation_id": "inv-b",
            "entity_id": "callee",
            "run_id": "run-b",
            "entry_id": "entry-b",
        },
        {
            "snapshot_id": "snap-other",
            "test_definition_id": "test-cross",
            "entity_id": "caller",
            "entry_id": "cross",
        },
    ]
    tools = _tools(runtime_records=records)
    observed = tools.find_observed_tests(_request(), entity_ids=("caller",))
    assert len(observed.results) == 1
    assert observed.results[0]["evidence_type"] == "RUNTIME_OBSERVATION"
    assert observed.results[0]["verified_graph_path"] is None
    assert observed.results[0]["evidence"]["type"] == "RUNTIME_OBSERVATION"
    plan = tools.build_test_plan(_request(), changed_symbol_ids=("caller", "callee"))
    assert [item["test_definition_id"] for item in plan.results] == ["test-a", "test-b"]
    assert plan.results[0]["covered_changed_symbol_ids"] == ["caller"]


def test_runtime_call_observations_preserve_exact_run_invocation_and_materialization() -> None:
    records = [
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-repeat",
            "test_invocation_id": "invocation-repeat",
            "source_entity_id": "caller",
            "entity_id": "base-run",
            "observation_kind": "CALL_OBSERVATION",
            "observation_id": "observation-a",
            "run_id": "run-1",
            "runtime_materialization_id": "materialization-a",
            "runtime_environment_id": "environment-a",
            "site_id": "call-known",
        },
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-repeat",
            "test_invocation_id": "invocation-repeat",
            "source_entity_id": "caller",
            "entity_id": "callee",
            "observation_kind": "CALL_OBSERVATION",
            "observation_id": "observation-c",
            "run_id": "run-2",
            "runtime_materialization_id": "materialization-a",
            "runtime_environment_id": "environment-a",
            "site_id": "call-known",
        },
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-repeat",
            "test_invocation_id": "invocation-repeat",
            "source_entity_id": "derived-run",
            "entity_id": "callee",
            "observation_kind": "CALL_OBSERVATION",
            "observation_id": "observation-b",
            "run_id": "run-1",
            "runtime_materialization_id": "materialization-b",
            "runtime_environment_id": "environment-b",
            "site_id": "call-unknown",
        },
        {
            "snapshot_id": SNAPSHOT,
            "test_definition_id": "test-repeat",
            "test_invocation_id": "invocation-repeat",
            "source_entity_id": "caller",
            "entity_id": "base-run",
            "observation_kind": "CALL_OBSERVATION",
            "observation_id": "observation-a",
            "run_id": "run-1",
            "runtime_materialization_id": "materialization-a",
            "runtime_environment_id": "environment-a",
            "site_id": "call-known",
        },
    ]
    result = _tools(runtime_records=records).find_observed_tests(_request())
    assert len(result.results) == 3
    call_records = [call for record in result.results for call in record["call_observations"]]

    assert len(call_records) == 3
    assert {
        (
            item["source_entity_id"],
            item["target_entity_id"],
            item["run_id"],
            item["test_invocation_id"],
            item["runtime_materialization_id"],
            item["observation_id"],
            item["site_id"],
        )
        for item in call_records
    } == {
        (
            "caller",
            "base-run",
            "run-1",
            "invocation-repeat",
            "materialization-a",
            "observation-a",
            "call-known",
        ),
        (
            "derived-run",
            "callee",
            "run-1",
            "invocation-repeat",
            "materialization-b",
            "observation-b",
            "call-unknown",
        ),
        (
            "caller",
            "callee",
            "run-2",
            "invocation-repeat",
            "materialization-a",
            "observation-c",
            "call-known",
        ),
    }
    assert all(len(record["run_ids"]) == 1 for record in result.results)
    assert all(len(record["entity_ids"]) == 2 for record in result.results)
    assert all(item["evidence"]["snapshot_id"] == SNAPSHOT for item in call_records)
    assert all(
        item["evidence"]["source_record_id"] == item["observation_id"] for item in call_records
    )
    assert GraphToolResult.model_validate_json(result.model_dump_json()) == result


def test_high_fanout_callers_keep_exact_count_with_bounded_ordered_results() -> None:
    analysis = _analysis()
    sites = list(analysis.sites)
    bindings = list(analysis.bindings)
    fanout = 2_000
    for index in range(fanout):
        site_id = f"fanout-{index:05d}"
        sites.append(
            SourceSite(
                site_id=site_id,
                kind=SiteKind.CALL_CALLEE,
                expression="callee",
                file_path="a.py",
                span=_span(5, 11),
                context_id="caller",
                syntax_node_type="identifier",
            )
        )
        bindings.append(
            SemanticBinding(
                binding_id=f"fanout-binding-{index:05d}",
                edge_id=f"fanout-edge-{index:05d}",
                site_id=site_id,
                target_entity_id="callee",
                scip_symbol="b.callee.",
                occurrence_start_byte=5,
            )
        )
    analysis = analysis.model_copy(update={"sites": sites, "bindings": bindings})

    result = DeterministicGraphTools(analysis).find_callers(
        GraphToolRequest(snapshot_id=SNAPSHOT, limit=17), "callee"
    )

    assert result.candidate_count_before_limit == fanout + 2
    assert len(result.results) == 17
    assert result.truncated
    assert [item["site_id"] for item in result.results] == sorted(
        item["site_id"] for item in result.results
    )


def test_text_search_is_snapshot_scoped_and_never_returns_a_graph_path() -> None:
    passages = [
        {**item.model_dump(mode="json"), "score": score}
        for item, score in zip(_analysis().retrieval_chunks, (0.8, 0.9), strict=True)
    ]
    stale_hit = {**passages[0], "source_node_id": "forged-source", "score": 1.0}
    passages.insert(0, stale_hit)
    passages.append(
        {
            "snapshot_id": "snap-other",
            "chunk_id": "wrong",
            "chunk_kind": "FUNCTION_BODY",
            "score": 1.0,
        }
    )
    tools = _tools(
        lexical_search=lambda _query, _limit: passages, semantic_search=lambda _q, _l: passages
    )
    code = tools.search_code_text(_request(), "function")
    assert [item["chunk_id"] for item in code.results] == ["code-a"]
    semantic = tools.search_semantic_chunks(_request(), "function")
    assert {item["chunk_id"] for item in semantic.results} == {"code-a", "doc-a"}
    assert all(item["verified_graph_path"] is None for item in semantic.results)
    assert all(item["evidence"]["type"] == "SEMANTIC_PASSAGE" for item in semantic.results)
    assert semantic.results[0]["evidence"]["snapshot_id"] == SNAPSHOT
    assert any("Rejected" in item for item in semantic.diagnostics)
    assert not tools.find_paths(
        PathToolRequest(
            snapshot_id=SNAPSHOT,
            source_id="doc-source-a",
            target_id="caller",
        )
    ).results


def test_non_ready_snapshot_is_not_queryable() -> None:
    analysis = _analysis().model_copy(update={"status": "FAILED"})
    try:
        DeterministicGraphTools(analysis)
    except KeyError as exc:
        assert "READY" in str(exc)
    else:
        raise AssertionError("non-ready snapshot must be rejected")


def test_snapshot_readiness_is_rechecked_for_live_store_adapters() -> None:
    tools = DeterministicGraphTools(_analysis(), snapshot_guard=lambda _snapshot: False)
    try:
        tools.resolve_symbol(_request(), "caller")
    except KeyError as exc:
        assert "READY" in str(exc)
    else:
        raise AssertionError("snapshot whose status changed must not be queried")


def test_result_limit_metadata_ordering_and_request_bounds_are_explicit() -> None:
    tools = _tools()
    result = tools.find_overrides(_request(limit=1))
    assert result.candidate_count_before_limit == 1
    assert not result.truncated
    assert result.ordering == ("source_id", "target_id", "edge_id")
    try:
        GraphToolRequest(snapshot_id=SNAPSHOT, limit=501)
    except ValueError:
        pass
    else:
        raise AssertionError("tool result limit must be bounded")


def test_every_tool_enforces_snapshot_scope_and_returns_bounded_result_metadata() -> None:
    analysis = _analysis()
    chunks = [
        {
            **item.model_dump(mode="json"),
            "score": 0.5,
        }
        for item in analysis.retrieval_chunks
    ]
    tools = DeterministicGraphTools(
        analysis,
        source_loader=lambda _path: b"a" * 20,
        replay=lambda edge_id: ReplayResult(
            edge_id=edge_id,
            scope=ReplayScope.FILE_OR_SPAN,
            outcome=ReplayOutcome.PASS,
            review_state=ReviewState.NONE,
            message="Evidence replay passed.",
        ),
        runtime_records=(
            {
                "snapshot_id": SNAPSHOT,
                "test_definition_id": "test-a",
                "test_invocation_id": "inv-a",
                "entity_id": "caller",
                "run_id": "run-a",
                "entry_id": "entry-a",
            },
        ),
        lexical_search=lambda _query, _limit: chunks,
        semantic_search=lambda _query, _limit: chunks,
    )
    path_request = PathToolRequest(
        snapshot_id=SNAPSHOT,
        source_id="caller",
        target_id="callee",
        limit=2,
    )
    calls = [
        lambda req: tools.resolve_symbol(req, "caller"),
        lambda req: tools.get_source_span(req, "a.py", 2, 6),
        lambda req: tools.find_references(req, "base"),
        lambda req: tools.find_call_sites(req, context_id="caller"),
        lambda req: tools.find_callers(req, "callee"),
        lambda req: tools.find_callees(req, "call-known"),
        lambda req: tools.find_importers(req, "mod-b"),
        lambda req: tools.find_inheritance(req, "derived"),
        lambda req: tools.find_overrides(req, "derived-run"),
        lambda req: tools.find_dispatch_candidates(req, "call-known"),
        lambda req: tools.find_paths(
            path_request.model_copy(update={"snapshot_id": req.snapshot_id})
        ),
        lambda req: tools.find_shortest_evidence_path(
            path_request.model_copy(update={"snapshot_id": req.snapshot_id})
        ),
        lambda req: tools.expand_neighborhood(req, "caller"),
        lambda req: tools.get_edge_provenance(req, "edge-bind-callee"),
        lambda req: tools.replay_evidence(req, "edge-bind-callee"),
        lambda req: tools.get_git_history(req),
        lambda req: tools.find_changed_paths(req),
        lambda req: tools.find_changed_symbols(req),
        lambda req: tools.find_observed_tests(req, entity_ids=("caller",)),
        lambda req: tools.build_test_plan(req, changed_symbol_ids=("caller",)),
        lambda req: tools.search_code_text(req, "function"),
        lambda req: tools.search_semantic_chunks(req, "function"),
    ]
    for call in calls:
        result = call(_request(limit=2))
        assert result.snapshot_id == SNAPSHOT
        assert result.result_limit == 2
        assert result.candidate_count_before_limit >= len(result.results)
        assert len(result.results) <= 2
        try:
            call(_request(snapshot_id="snap-wrong", limit=2))
        except KeyError as exc:
            assert "snapshot" in str(exc)
        else:
            raise AssertionError(f"{result.tool} accepted a cross-snapshot request")


@pytest.mark.asyncio
async def test_mcp_read_only_tools_match_service_and_rest_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.encoders import jsonable_encoder
    from fastapi.testclient import TestClient
    from mcp import Client

    from backend.app import main as api
    from backend.app.mcp_server import create_mcp_server
    from backend.app.retrieval import resolve_symbol
    from backend.app.tool_services import SnapshotToolContext

    analysis = _analysis()
    graph_tools = _tools()
    context = SnapshotToolContext(analysis=analysis, tools=graph_tools)
    server = create_mcp_server(lambda _snapshot_id: context)
    graph_request = _request()
    expected = {
        "resolve_symbol": (
            {"snapshot_id": SNAPSHOT, "query": "callee", "limit": 100},
            resolve_symbol(analysis, "callee"),
        ),
        "find_callers": (
            {"snapshot_id": SNAPSHOT, "target_id": "callee", "limit": 100},
            graph_tools.find_callers(graph_request, "callee"),
        ),
        "find_references": (
            {"snapshot_id": SNAPSHOT, "target_id": "base", "limit": 100},
            graph_tools.find_references(graph_request, "base"),
        ),
        "find_paths": (
            {
                "snapshot_id": SNAPSHOT,
                "source_id": "caller",
                "target_id": "callee",
                "hop_limit": 3,
                "limit": 100,
            },
            graph_tools.find_paths(
                PathToolRequest(snapshot_id=SNAPSHOT, source_id="caller", target_id="callee")
            ),
        ),
        "find_changed_symbols": (
            {"snapshot_id": SNAPSHOT, "limit": 100},
            graph_tools.find_changed_symbols(graph_request),
        ),
        "find_observed_tests": (
            {"snapshot_id": SNAPSHOT, "entity_ids": [], "limit": 100},
            graph_tools.find_observed_tests(graph_request),
        ),
        "get_edge_provenance": (
            {"snapshot_id": SNAPSHOT, "edge_id": "edge-bind-callee", "limit": 100},
            graph_tools.get_edge_provenance(graph_request, "edge-bind-callee"),
        ),
        "search_code_text": (
            {"snapshot_id": SNAPSHOT, "query": "caller", "limit": 100},
            graph_tools.search_code_text(graph_request, "caller"),
        ),
        "search_semantic_chunks": (
            {"snapshot_id": SNAPSHOT, "query": "caller", "limit": 100},
            graph_tools.search_semantic_chunks(graph_request, "caller"),
        ),
        "build_test_plan": (
            {"snapshot_id": SNAPSHOT, "changed_symbol_ids": ["caller"], "limit": 100},
            graph_tools.build_test_plan(graph_request, ["caller"]),
        ),
    }

    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == set(expected)
        assert "run_approved_tests" not in names
        assert all(
            tool.annotations is not None
            and tool.annotations.model_dump(by_alias=True).get("readOnlyHint") is True
            for tool in listed.tools
        )

        for name, (arguments, service_result) in expected.items():
            rest_normalized = jsonable_encoder(service_result)
            response = await client.call_tool(name, arguments)
            assert response.is_error is False, (name, response.content)
            assert response.structured_content == rest_normalized

    monkeypatch.setattr(api, "load_snapshot_tool_context", lambda *_args, **_kwargs: context)
    with TestClient(api.app) as rest:
        rest_resolution = rest.get(
            f"/api/snapshots/{SNAPSHOT}/symbols/resolve",
            params={"query": "callee", "limit": 100},
        )
        assert rest_resolution.status_code == 200
        assert rest_resolution.json() == jsonable_encoder(expected["resolve_symbol"][1])

        for tool_name, path, params in (
            (
                "find_callers",
                f"/api/snapshots/{SNAPSHOT}/graph/callers",
                {"target_id": "callee", "limit": 100},
            ),
            (
                "find_references",
                f"/api/snapshots/{SNAPSHOT}/graph/references",
                {"target_id": "base", "limit": 100},
            ),
        ):
            response = rest.get(path, params=params)
            assert response.status_code == 200
            assert response.json() == jsonable_encoder(expected[tool_name][1])


@pytest.mark.asyncio
async def test_mcp_rejects_malformed_ids_hops_and_snapshot_mismatch() -> None:
    from mcp import Client

    from backend.app.mcp_server import create_mcp_server
    from backend.app.models import SnapshotStatus
    from backend.app.tool_services import SnapshotToolContext

    context = SnapshotToolContext(analysis=_analysis(), tools=_tools())
    server = create_mcp_server(lambda _snapshot_id: context)
    async with Client(server) as client:
        bad_id = await client.call_tool(
            "find_callers", {"snapshot_id": SNAPSHOT, "target_id": "caller/../callee"}
        )
        assert bad_id.is_error
        bad_hops = await client.call_tool(
            "find_paths",
            {
                "snapshot_id": SNAPSHOT,
                "source_id": "caller",
                "target_id": "callee",
                "hop_limit": 6,
            },
        )
        assert bad_hops.is_error
        bad_limit = await client.call_tool(
            "find_references",
            {"snapshot_id": SNAPSHOT, "target_id": "base", "limit": 501},
        )
        assert bad_limit.is_error

        stale_server = create_mcp_server(lambda _snapshot_id: context)
        # The loader is called with the submitted snapshot and its returned context is checked.
        async with Client(stale_server) as stale_client:
            mismatch = await stale_client.call_tool(
                "find_callers", {"snapshot_id": "other-snapshot", "target_id": "callee"}
            )
            assert mismatch.is_error

        failed = SnapshotToolContext(
            analysis=_analysis().model_copy(update={"status": SnapshotStatus.FAILED}),
            tools=_tools(),
        )
        failed_server = create_mcp_server(lambda _snapshot_id: failed)
        async with Client(failed_server) as failed_client:
            not_ready = await failed_client.call_tool(
                "find_callers", {"snapshot_id": SNAPSHOT, "target_id": "callee"}
            )
            assert not_ready.is_error

from __future__ import annotations

import asyncio
import math
import os
import platform
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import Settings
from backend.app.diff_analysis import CommitDiffService, DiffAnalysisRequest
from backend.app.embeddings import EmbeddedVector, EmbeddingMeasurement, EmbeddingSpec
from backend.app.fusion import (
    EvidenceType,
    RetrievalChannel,
    fuse_passage_ranks,
    passage_from_graph_record,
)
from backend.app.graph import GraphStore
from backend.app.graph_tools import DeterministicGraphTools, GraphToolRequest
from backend.app.ids import stable_id
from backend.app.ingestion import analyze_repository, load_snapshot_analysis
from backend.app.investigation import build_investigation_response
from backend.app.investigation_memory import InvestigationMemoryService
from backend.app.jobs import IngestionJobManager
from backend.app.main import QueryExecutionRequest
from backend.app.materialization import runtime_files_root
from backend.app.models import (
    IngestionJob,
    IngestionJobStatus,
    RuntimeContext,
    RuntimeEnvironment,
    RuntimeRunStatus,
    SnapshotAnalysis,
    SnapshotStatus,
)
from backend.app.models import (
    TestDefinition as RuntimeTestDefinition,
)
from backend.app.models import (
    TestExecutionResult as RuntimeTestExecutionResult,
)
from backend.app.models import (
    TestInvocation as RuntimeTestInvocation,
)
from backend.app.orchestrator import QueryOrchestrator
from backend.app.provenance import replay_edge
from backend.app.schema import validate_analysis
from backend.app.test_execution import DockerTestExecutor, RuntimeActionService
from backend.tests.test_ingestion import init_repo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCIP_PYTHON = os.environ.get("SCIP_PYTHON_BIN")
SCIP_CLI = os.environ.get("SCIP_CLI_BIN")

pytestmark = pytest.mark.skipif(
    os.environ.get("FALKORDB_INTEGRATION") != "1" or not (SCIP_PYTHON and SCIP_CLI),
    reason="requires Compose FalkorDB and real SCIP binaries",
)


def test_real_publish_query_and_persistence_after_container_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "README.md").write_text(
        "# M16 retrieval fixture\n\nFalkorDB lexical fusion sentinel passage.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "fixture: add retrieval passage"],
        check=True,
    )
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )
    analysis = analyze_repository(repo, settings)
    assert analysis.analysis_environment is not None
    validate_analysis(analysis)
    store = GraphStore(settings)
    repository_id = store.register_repository(analysis.repository)
    registered = store.metadata_graph().query(
        "MATCH (r:Repository {repository_id: $repository_id}) "
        "RETURN r.head_commit, r.is_dirty, r.is_shallow, r.is_detached",
        {"repository_id": repository_id},
    )
    assert registered.result_set == [[analysis.repository.head_commit, False, False, False]]

    in_progress = [
        IngestionJob(
            job_id=stable_id("restart-probe", status.value, prefix="job_"),
            snapshot_id=(
                analysis.snapshot_id
                if status == IngestionJobStatus.SEMANTIC_INDEXING
                else stable_id("restart-probe", status.value, prefix="snap_")
            ),
            status=status,
        )
        for status in (
            IngestionJobStatus.QUEUED,
            IngestionJobStatus.MATERIALIZING,
            IngestionJobStatus.PARSING,
            IngestionJobStatus.SEMANTIC_INDEXING,
            IngestionJobStatus.VALIDATING,
            IngestionJobStatus.PUBLISHING,
        )
    ]
    for interrupted_job in in_progress:
        store.update_ingestion_job(interrupted_job, analysis.repository)
    recovered_ids = store.recover_interrupted_ingestion_jobs()
    assert {job.job_id for job in in_progress} <= set(recovered_ids)
    assert store.snapshot_status(analysis.snapshot_id) == "FAILED"

    write_snapshot = store._write_snapshot

    def write_then_fail(snapshot: SnapshotAnalysis) -> str:
        write_snapshot(snapshot)
        raise RuntimeError("injected failure after writing snapshot data")

    monkeypatch.setattr(store, "_write_snapshot", write_then_fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        store.publish_snapshot(analysis)
    failed_graph_name = (
        store.metadata_graph()
        .query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) RETURN s.graph_name",
            {"snapshot_id": analysis.snapshot_id},
        )
        .result_set[0][0]
    )
    failed_graph = store.client.select_graph(failed_graph_name)
    assert store.snapshot_status(analysis.snapshot_id) == "FAILED"
    assert failed_graph.query("MATCH (n:SourceEntity) RETURN count(n)").result_set[0][0] > 0
    with pytest.raises(KeyError, match="Only READY"):
        store.resolve_exact(analysis.snapshot_id, "pkg.auth.verify_token")
    monkeypatch.undo()

    retry_manager = IngestionJobManager()
    retry = retry_manager.submit(repo, settings)
    assert retry.snapshot_id == analysis.snapshot_id
    assert retry.job_id != next(
        job.job_id for job in in_progress if job.snapshot_id == analysis.snapshot_id
    )
    retried = retry_manager.wait(analysis.snapshot_id, timeout=180)
    assert retried.status == IngestionJobStatus.COMPLETED, retried.model_dump()
    assert store.snapshot_status(analysis.snapshot_id) == "READY"
    snapshot_job_status = (
        store.metadata_graph()
        .query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) RETURN s.job_status",
            {"snapshot_id": analysis.snapshot_id},
        )
        .result_set[0][0]
    )
    assert snapshot_job_status == IngestionJobStatus.COMPLETED.value
    assert analysis.snapshot_id not in store.recover_interrupted_ingestion_jobs()
    assert store.snapshot_status(analysis.snapshot_id) == "READY"
    listed = next(
        item
        for item in store.list_snapshots(limit=50)
        if item["snapshot_id"] == analysis.snapshot_id
    )
    assert listed["repository_id"] == analysis.repository.repository_id
    assert listed["repository_root"] == str(repo.resolve())
    assert listed["status"] == "READY"

    from backend.app import main

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "GraphStore", lambda _settings: store)
    investigation = asyncio.run(
        main.query_investigate(
            QueryExecutionRequest(
                question="Who are the callers of `pkg.auth.verify_token`?",
                snapshot_id=analysis.snapshot_id,
            )
        )
    )
    assert investigation.snapshot_id == analysis.snapshot_id
    assert investigation.investigation_id is not None
    assert (
        InvestigationMemoryService(store)
        .get(investigation.investigation_id, snapshot_id=analysis.snapshot_id)
        .latest_result.answer
        == investigation.answer
    )
    assert investigation.query.evidence_pool.snapshot_id == analysis.snapshot_id
    assert all(
        claim.status.value in {"SUPPORTED", "QUALIFIED"} and claim.evidence
        for claim in investigation.answer.claims
    )

    graph_name = store.publish_snapshot(analysis)
    graph = store.client.select_graph(graph_name)
    module_by_id = {item.entity_id: item for item in analysis.entities if item.kind == "Module"}
    local_import = next(
        item
        for item in analysis.imports
        if item.imported_module == "pkg.common"
        and module_by_id[item.source_module_id].qualified_name == "pkg.imports"
    )
    stored_import = graph.query(
        "MATCH (source:SourceEntity)-[r:IMPORTS {edge_id: $edge_id}]->"
        "(target:SourceEntity) RETURN source.qualified_name, target.qualified_name, "
        "r.file_path, r.start_byte, r.end_byte, r.resolver",
        {"edge_id": local_import.edge_id},
    )
    assert stored_import.result_set == [
        [
            "pkg.imports",
            "pkg.common",
            "pkg/imports.py",
            local_import.span.start_byte,
            local_import.span.end_byte,
            "repository-module-index",
        ]
    ]
    semantic_record = next(
        item for item in analysis.provenance if item.extractor_name == "scip-python"
    )
    stored_provenance = graph.query(
        "MATCH (p:ProvenanceRecord {provenance_id: $provenance_id}) "
        "RETURN p.repository_id, p.analysis_environment_id, p.start_byte, p.end_byte, "
        "p.start_line, p.start_column, p.end_line, p.end_column, "
        "p.source_coordinate_encoding, p.created_at, p.scip_document, p.scip_symbol, "
        "p.scip_position_encoding",
        {"provenance_id": semantic_record.provenance_id},
    ).result_set
    assert stored_provenance and stored_provenance[0][0] == analysis.repository.repository_id
    assert stored_provenance[0][1] == analysis.analysis_environment.environment_id
    assert stored_provenance[0][2:9] == [
        semantic_record.span.start_byte,
        semantic_record.span.end_byte,
        semantic_record.span.start_line,
        semantic_record.span.start_column,
        semantic_record.span.end_line,
        semantic_record.span.end_column,
        "UTF-8_BYTE_OFFSETS",
    ]
    assert stored_provenance[0][9] is not None
    target_chunk = next(
        chunk for chunk in analysis.retrieval_chunks if chunk.file_path == "README.md"
    )
    target_chunk_id = target_chunk.chunk_id
    probe_model = f"falkordb-vector-probe-{target_chunk_id}"
    vector_spec = EmbeddingSpec(provider="integration", model=probe_model, dimensions=3)
    golden_angle = math.pi * (3.0 - math.sqrt(5.0))
    chunk_vectors = []
    for index, chunk in enumerate(analysis.retrieval_chunks):
        z = 1.0 - (2.0 * (index + 1) / (len(analysis.retrieval_chunks) + 1))
        radius = math.sqrt(1.0 - z * z)
        theta = golden_angle * index
        probe_vector = (radius * math.cos(theta), radius * math.sin(theta), z)
        if chunk.chunk_id == target_chunk_id:
            probe_vector = (1.0, 0.0, 0.0)
        chunk_vectors.append(
            EmbeddedVector(
                chunk_id=chunk.chunk_id,
                snapshot_id=analysis.snapshot_id,
                spec_id=vector_spec.identity,
                content_hash=chunk.content_hash,
                vector=probe_vector,
            )
        )
    store.store_chunk_embeddings(
        analysis.snapshot_id,
        EmbeddingMeasurement(
            spec=vector_spec, vectors=chunk_vectors, cache_hits=0, elapsed_seconds=0.0
        ),
    )
    target_label = store._analysis_chunks(analysis.snapshot_id)[target_chunk_id]
    target_vector = graph.query(
        f"MATCH (c:{target_label} {{chunk_id: $chunk_id}}) "
        f"RETURN c.{store._embedding_property(vector_spec.identity)}",
        {"chunk_id": target_chunk_id},
    ).result_set[0][0]
    assert tuple(float(value) for value in target_vector) == (1.0, 0.0, 0.0)
    vector_hits = store.query_chunk_embeddings(
        analysis.snapshot_id, vector_spec.identity, [1.0, 0.0, 0.0], limit=1
    )
    assert vector_hits
    assert vector_hits[0]["chunk_id"] == target_chunk_id, vector_hits
    assert vector_hits[0]["source_node_id"] == target_chunk.source_node_id
    assert vector_hits[0]["score"] > 0.99
    assert vector_hits[0]["type"] == "SEMANTIC_PASSAGE"
    assert vector_hits[0]["verified_graph_path"] is None
    lexical_hits = store.query_chunk_fulltext(analysis.snapshot_id, "sentinel", limit=10)
    assert any(item["chunk_id"] == target_chunk_id for item in lexical_hits)
    fused = fuse_passage_ranks(
        analysis.snapshot_id,
        [passage_from_graph_record(item) for item in lexical_hits],
        [passage_from_graph_record(item) for item in vector_hits],
        exact_entity_seed_ids=["mandatory-exact-seed"],
    )
    fused_target = next(item for item in fused.items if item.chunk_id == target_chunk_id)
    assert {signal.channel for signal in fused_target.retrieval_signals} == {
        RetrievalChannel.LEXICAL,
        RetrievalChannel.VECTOR,
    }
    assert fused.mandatory_seed_entity_ids == ("mandatory-exact-seed",)
    assert not hasattr(fused_target, "verified_graph_path")
    before_vector_mutation = graph.query(
        "MATCH ()-[r]->() RETURN count(r), count(DISTINCT r.edge_id)"
    ).result_set[0]
    assert len(analysis.retrieval_chunks) > 0
    assert before_vector_mutation[0] == before_vector_mutation[1]
    before_idempotent_load = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )
    assert store.publish_snapshot(analysis) == graph_name
    after_idempotent_load = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )
    assert after_idempotent_load == before_idempotent_load
    indexes = graph.query("CALL db.indexes()")
    assert any(row[0] == "Method" and row[1] == ["id"] for row in indexes.result_set)
    assert store.snapshot_status(analysis.snapshot_id) == "READY"
    persisted_relations = graph.query("MATCH ()-[r:INHERITS]->() RETURN count(r)").result_set
    assert persisted_relations[0][0] > 0
    typed_methods = graph.query("MATCH (m:SourceEntity:Method) RETURN count(m)").result_set
    assert typed_methods[0][0] > 0
    method_edges = graph.query(
        "MATCH (:Class)-[r:DEFINES_METHOD]->(:Method) RETURN count(r)"
    ).result_set
    assert method_edges[0][0] > 0
    dispatch_site = next(
        site
        for site in analysis.sites
        if site.file_path == "pkg/base_dispatch.py" and site.expression == "child.run"
    )
    dispatch_status = graph.query(
        "MATCH (c:CallSite {id: $site_id}) RETURN c.dispatch_state, c.mro_qualified_names",
        {"site_id": dispatch_site.site_id},
    ).result_set
    assert dispatch_status == [
        [
            "SINGLE_CANDIDATE",
            [
                "pkg.base_dispatch.Child",
                "pkg.base_dispatch.Left",
                "pkg.base_dispatch.Right",
                "pkg.base_dispatch.Grandparent",
                "object",
            ],
        ]
    ]
    semantic_record = next(
        item for item in analysis.provenance if item.extractor_name == "scip-python"
    )
    replay_result = replay_edge(analysis, semantic_record.edge_id, settings)
    store.update_provenance_replay(
        analysis.snapshot_id,
        semantic_record.provenance_id,
        semantic_record.model_copy(
            update={
                "replay_state": replay_result.outcome,
                "review_state": replay_result.review_state,
            }
        ),
    )
    replay_state = graph.query(
        "MATCH (p:ProvenanceRecord {provenance_id: $provenance_id}) "
        "RETURN p.replay_state, p.review_state",
        {"provenance_id": semantic_record.provenance_id},
    ).result_set
    assert replay_state == [["PASS", "NONE"]]

    resolved = store.resolve_exact(analysis.snapshot_id, "pkg.auth.verify_token")
    assert resolved["results"], resolved
    assert "id" in resolved["results"][0], resolved
    target_id = resolved["results"][0]["id"]
    callers = store.find_callers(analysis.snapshot_id, target_id)
    assert any(item["expression"] == "check" for item in callers["results"])
    excluded_edge_id = next(
        item["edge_id"] for item in callers["results"] if item["expression"] == "check"
    )
    filtered_callers = store.find_callers(
        analysis.snapshot_id, target_id, excluded_edge_ids=(excluded_edge_id,)
    )
    assert excluded_edge_id not in {item["edge_id"] for item in filtered_callers["results"]}
    assert "replay integrity failures were excluded" in " ".join(filtered_callers["limitations"])

    runtime_environment = RuntimeEnvironment(
        runtime_environment_id=f"runtime_{analysis.snapshot_id}",
        snapshot_id=analysis.snapshot_id,
        python_version=platform.python_version(),
        platform="linux",
        python_executable_hash="python-hash",
        sys_path_hash="sys-path-hash",
        installed_distributions_hash="distributions-hash",
        source_manifest_hash="runtime-materialization-hash",
        runtime_materialization_id=f"materialization_{analysis.snapshot_id}",
    )
    test_definition = RuntimeTestDefinition(
        test_definition_id=f"definition_{analysis.snapshot_id}",
        snapshot_id=analysis.snapshot_id,
        file_path="tests/test_runtime_probe.py",
        qualified_name="test_runtime_probe",
        pytest_name="test_runtime_probe",
    )
    test_invocation = RuntimeTestInvocation(
        test_invocation_id=f"invocation_{analysis.snapshot_id}",
        snapshot_id=analysis.snapshot_id,
        test_definition_id=test_definition.test_definition_id,
        node_id="tests/test_runtime_probe.py::test_runtime_probe",
    )
    mismatch_target = next(
        entity.entity_id
        for entity in analysis.entities
        if entity.kind in {"Function", "Method"}
        and entity.entity_id != dispatch_site.context_id
        and entity.entity_id
        not in next(
            state.candidate_entity_ids
            for state in analysis.dispatch
            if state.site_id == dispatch_site.site_id
        )
    )
    runtime_result = RuntimeTestExecutionResult(
        run_id=f"run_{analysis.snapshot_id}",
        snapshot_id=analysis.snapshot_id,
        runtime_environment_id=runtime_environment.runtime_environment_id,
        status=RuntimeRunStatus.COMPLETED,
        started_at=time.time(),
        ended_at=time.time(),
        runtime_entries=[
            {
                "entity_id": target_id,
                "test_invocation_id": test_invocation.test_invocation_id,
                "context": RuntimeContext.TEST_INVOCATION.value,
            }
        ],
        runtime_calls=[
            {
                "source_entity_id": dispatch_site.context_id,
                "target_entity_id": mismatch_target,
                "site_id": dispatch_site.site_id,
                "test_invocation_id": test_invocation.test_invocation_id,
                "context": RuntimeContext.TEST_INVOCATION.value,
            }
        ],
        runtime_diagnostics=[
            {
                "site_id": dispatch_site.site_id,
                "code": "RUNTIME_MISMATCH",
                "message": "Runtime observed a target outside the static candidate set.",
                "file_path": dispatch_site.file_path,
                "qualified_name": dispatch_site.expression,
                "first_line": dispatch_site.span.start_line,
            }
        ],
    )
    store.persist_runtime_observations(
        analysis,
        runtime_environment,
        runtime_result,
        [test_definition],
        [test_invocation],
    )
    tools = DeterministicGraphTools.from_store(analysis, store)
    observed_tests = tools.find_observed_tests(
        GraphToolRequest(snapshot_id=analysis.snapshot_id), entity_ids=(target_id,)
    )
    assert [item["test_definition_id"] for item in observed_tests.results] == [
        test_definition.test_definition_id
    ]
    assert observed_tests.results[0]["evidence_type"] == "RUNTIME_OBSERVATION"
    assert observed_tests.results[0]["verified_graph_path"] is None
    call_observations = observed_tests.results[0]["call_observations"]
    assert len(call_observations) == 1
    assert call_observations[0]["runtime_materialization_id"] == (
        runtime_environment.runtime_materialization_id
    )
    assert call_observations[0]["source_entity_id"] == dispatch_site.context_id
    assert call_observations[0]["target_entity_id"] == mismatch_target
    test_plan = tools.build_test_plan(
        GraphToolRequest(snapshot_id=analysis.snapshot_id), changed_symbol_ids=(target_id,)
    )
    assert [item["test_definition_id"] for item in test_plan.results] == [
        test_definition.test_definition_id
    ]
    completed_run = graph.query(
        "MATCH (run:RuntimeRun {run_id: $run_id}) RETURN run.status",
        {"run_id": runtime_result.run_id},
    ).result_set
    assert completed_run == [["COMPLETED"]]

    original_query = graph.query
    original_snapshot_graph = store.snapshot_graph
    monkeypatch.setattr(store, "snapshot_graph", lambda _snapshot_id: (graph, graph_name))
    failure_points = (
        ("RuntimeFunctionEntry", "entry"),
        ("RuntimeCallObservation", "call"),
        ("RuntimeDiagnostic", "diagnostic"),
        ("SET r.status = $status", "finalize"),
    )
    for needle, failure_name in failure_points:
        interrupted_result = runtime_result.model_copy(
            update={"run_id": f"run_{failure_name}_failure"}
        )

        def fail_runtime_persistence(
            query: str,
            params: dict[str, Any] | None = None,
            *,
            _needle: str = needle,
        ) -> Any:
            if _needle in query:
                raise RuntimeError("injected runtime persistence failure")
            return original_query(query, params or {})

        monkeypatch.setattr(graph, "query", fail_runtime_persistence)
        with pytest.raises(RuntimeError, match="injected runtime persistence failure"):
            store.persist_runtime_observations(
                analysis,
                runtime_environment,
                interrupted_result,
                [test_definition],
                [test_invocation],
            )
        monkeypatch.setattr(graph, "query", original_query)
        pending_status = graph.query(
            "MATCH (run:RuntimeRun {run_id: $run_id}) RETURN run.status",
            {"run_id": interrupted_result.run_id},
        ).result_set
        assert pending_status == [["PERSISTING"]]
        assert interrupted_result.run_id in store.recover_interrupted_runtime_persistence()
        failed_status = graph.query(
            "MATCH (run:RuntimeRun {run_id: $run_id}) RETURN run.status",
            {"run_id": interrupted_result.run_id},
        ).result_set
        assert failed_status == [["FAILED"]]
    monkeypatch.setattr(store, "snapshot_graph", original_snapshot_graph)
    runtime_links = (
        store.snapshot_graph(analysis.snapshot_id)[0]
        .query(
            "MATCH (run:RuntimeRun {run_id: $run_id, status: 'COMPLETED'}), "
            "(entry:RuntimeFunctionEntry)-[:OBSERVED_ENTITY]->(:SourceEntity), "
            "(entry)-[:DURING]->(:TestInvocation), "
            "(call:RuntimeCallObservation)-[:OBSERVED_FROM]->(:SourceEntity), "
            "(call)-[:OBSERVED_TO]->(:SourceEntity), (call)-[:DURING]->(:TestInvocation), "
            "(call)-[:OBSERVED_AT]->(:CallSite), "
            "(diagnostic:RuntimeDiagnostic)-[:ABOUT_SITE]->(:CallSite), "
            "(diagnostic:RuntimeDiagnostic)-[:OBSERVED_IN]->(:RuntimeRun) "
            "WHERE entry.run_id = run.run_id AND call.run_id = run.run_id "
            "AND diagnostic.run_id = run.run_id "
            "RETURN count(DISTINCT entry), count(DISTINCT call), count(DISTINCT diagnostic)",
            {"run_id": runtime_result.run_id},
        )
        .result_set
    )
    assert runtime_links == [[1, 1, 1]]
    mismatch_edge = next(
        edge
        for edge in analysis.derived_edges
        if edge.relation == "MAY_DISPATCH_TO" and edge.source_id == dispatch_site.site_id
    )
    mismatch_review = store.snapshot_graph(analysis.snapshot_id)[0].query(
        "MATCH (p:ProvenanceRecord {edge_id: $edge_id}) RETURN p.review_state",
        {"edge_id": mismatch_edge.edge_id},
    )
    assert mismatch_review.result_set == [["RUNTIME_MISMATCH"]]

    site_by_id = {site.site_id: site for site in analysis.sites}
    memory_source_id = next(
        site_by_id[binding.site_id].context_id
        for binding in analysis.bindings
        if binding.target_entity_id == target_id and binding.binding_state == "EXACT"
    )
    memory_question = f"Find paths from `{memory_source_id}` to `{target_id}`"
    memory_query = asyncio.run(
        QueryOrchestrator(DeterministicGraphTools.from_store(analysis, store)).execute(
            memory_question, snapshot_id=analysis.snapshot_id
        )
    )
    memory_response = build_investigation_response(
        memory_query, DeterministicGraphTools.from_store(analysis, store)
    )
    memory_service = InvestigationMemoryService(store)
    static_edge_count_before_note = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    memory_record = memory_service.save_result(
        memory_response, question=memory_question, selected_entities=()
    )
    memory_record = memory_service.add_note(
        memory_record.investigation_id,
        snapshot_id=analysis.snapshot_id,
        text="Review the exact binding provenance before the demo.",
    )
    static_edge_count_after_note = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    assert static_edge_count_after_note == static_edge_count_before_note
    assert memory_record.evidence_paths
    assert memory_record.validated_claims
    memory_pin = store.metadata_graph().query(
        "MATCH (i:Investigation {investigation_id: $investigation_id})-[:PINS]->"
        "(s:RepositorySnapshot) RETURN i.repository_id, s.snapshot_id",
        {"investigation_id": memory_record.investigation_id},
    )
    assert memory_pin.result_set == [[analysis.repository.repository_id, analysis.snapshot_id]]

    marker_id = f"integration_{analysis.snapshot_id}"
    store.metadata_graph().query(
        "MERGE (p:PersistenceProbe {id: $id}) SET p.graph_name = $graph_name",
        {"id": marker_id, "graph_name": graph_name},
    )
    subprocess.run(["docker", "compose", "restart", "falkordb"], cwd=PROJECT_ROOT, check=True)

    deadline = time.monotonic() + 60
    reconnected = False
    while time.monotonic() < deadline:
        try:
            reconnected = GraphStore(settings).ping()
        except Exception:
            reconnected = False
        if reconnected:
            break
        time.sleep(1)
    assert reconnected, "FalkorDB did not become reachable after restart"

    restarted = GraphStore(settings)
    persisted = restarted.metadata_graph().query(
        "MATCH (p:PersistenceProbe {id: $id}) RETURN p.graph_name",
        {"id": marker_id},
    )
    assert persisted.result_set == [[graph_name]]
    assert restarted.snapshot_graph(analysis.snapshot_id)[1] == "READY"
    reopened_memory = InvestigationMemoryService(restarted).get(
        memory_record.investigation_id, snapshot_id=analysis.snapshot_id
    )
    assert reopened_memory == memory_record
    assert reopened_memory.evidence_paths[0].snapshot_id == analysis.snapshot_id
    assert InvestigationMemoryService(restarted).list(analysis.snapshot_id)[0].investigation_id == (
        memory_record.investigation_id
    )
    assert restarted.resolve_exact(analysis.snapshot_id, "pkg.auth.verify_token")["results"]
    assert any(
        item["expression"] == "check"
        for item in restarted.find_callers(analysis.snapshot_id, target_id)["results"]
    )

    gc_repository_id = f"gc_{analysis.snapshot_id}"
    gc_ids = [f"gc_{analysis.snapshot_id}_{index}" for index in range(1, 11)]
    gc_graph_names: list[str] = []
    for index, snapshot_id in enumerate(gc_ids, start=1):
        graph_name = f"gc_graph_{analysis.snapshot_id}_{index}"
        gc_graph_names.append(graph_name)
        restarted.client.select_graph(graph_name).query(
            "CREATE (:GCTest {id: $id})", {"id": snapshot_id}
        )
        restarted.metadata_graph().query(
            "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) SET "
            "s.repository_id = $repository_id, s.graph_name = $graph_name, "
            "s.status = 'READY', s.ready_at = $ready_at",
            {
                "snapshot_id": snapshot_id,
                "repository_id": gc_repository_id,
                "graph_name": graph_name,
                "ready_at": float(index),
            },
        )
    restarted.set_snapshot_pinned(gc_ids[0])
    for label, key, index in (
        ("Investigation", "investigation_id", 1),
        ("EvaluationReport", "report_id", 2),
        ("DemoFixture", "fixture_id", 3),
    ):
        restarted.metadata_graph().query(
            f"MERGE (p:{label} {{{key}: $pin_id}}) "
            "WITH p MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
            "MERGE (p)-[:PINS]->(s)",
            {"pin_id": f"pin_{analysis.snapshot_id}_{index}", "snapshot_id": gc_ids[index]},
        )
    collected = restarted.garbage_collect_snapshots(now=time.time() + 100)
    assert collected == [gc_ids[4]]
    assert set(gc_graph_names[:4]).issubset(set(restarted.client.list_graphs()))
    assert gc_graph_names[4] not in restarted.client.list_graphs()
    assert set(gc_graph_names[5:]).issubset(set(restarted.client.list_graphs()))
    assert restarted.garbage_collect_snapshots(now=time.time() + 100) == []


def test_real_fulltext_identifier_search_is_snapshot_scoped(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )
    earlier_analysis = analyze_repository(repo, settings)
    store = GraphStore(settings)
    store.publish_snapshot(earlier_analysis)

    payment_file = repo / "pkg" / "payment.py"
    payment_file.write_text(
        "class PaymentProcessor:\n    def charge_token(self):\n        return True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "pkg/payment.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add payment fixture"], check=True)
    analysis = analyze_repository(repo, settings)
    store.publish_snapshot(analysis)

    exact = store.resolve_exact(analysis.snapshot_id, "PaymentProcessor")
    assert exact["status"] == "UNIQUE"
    assert exact["matched_by"] == "NAME"
    assert exact["results"][0]["identifier_search_text"] == "payment processor"

    split = store.resolve_exact(analysis.snapshot_id, "payment processor")
    assert split["matched_by"] == "NORMALIZED"
    assert [item["qualified_name"] for item in split["results"]] == ["pkg.payment.PaymentProcessor"]

    token = store.resolve_exact(analysis.snapshot_id, "token")
    assert token["matched_by"] == "FULL_TEXT"
    assert any(item["qualified_name"].endswith("charge_token") for item in token["results"])

    previous_snapshot = store.resolve_exact(earlier_analysis.snapshot_id, "PaymentProcessor")
    assert previous_snapshot["status"] == "NOT_FOUND"


def test_real_query_orchestrator_returns_snapshot_scoped_graph_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )
    analysis = analyze_repository(repo, settings)
    store = GraphStore(settings)
    store.publish_snapshot(analysis)
    tools = DeterministicGraphTools.from_store(analysis, store)

    result = asyncio.run(
        QueryOrchestrator(tools).execute(
            "Find references to verify_token",
            snapshot_id=analysis.snapshot_id,
        )
    )

    assert result.plan.snapshot_id == analysis.snapshot_id
    assert result.evidence_pool.snapshot_id == analysis.snapshot_id
    references = [
        execution
        for execution in result.tool_executions
        if execution.request and execution.request.tool.value == "find_references"
    ]
    assert references
    assert references[0].result.results
    assert all(item.item.snapshot_id == analysis.snapshot_id for item in result.evidence_pool.items)
    assert any(item.item.type == EvidenceType.STATIC_BINDING for item in result.evidence_pool.items)


def test_real_commit_diff_ingests_both_snapshots_and_collects_tests_in_docker(
    tmp_path: Path, runtime_image: str
) -> None:
    repo = tmp_path / "m21-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "pkg").mkdir()
    (repo / "tests").mkdir()
    (repo / "pkg" / "module.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_module.py").write_text(
        "from pkg.module import changed\n\ndef test_changed():\n    assert changed() == 2\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=M21 Integration",
            "-c",
            "user.email=m21@example.test",
            "add",
            "-A",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=M21 Integration",
            "-c",
            "user.email=m21@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "pkg" / "module.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=M21 Integration",
            "-c",
            "user.email=m21@example.test",
            "commit",
            "-qm",
            "target",
        ],
        check=True,
    )
    target_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "uncommitted-local.py").write_text(
        "raise RuntimeError('live working-tree content must not enter commit snapshots')\n",
        encoding="utf-8",
    )

    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
        runtime_container_image=runtime_image,
    )
    store = GraphStore(settings)
    executor = DockerTestExecutor(settings)

    def persist_runtime(
        environment: RuntimeEnvironment,
        execution: RuntimeTestExecutionResult,
        definitions: list[RuntimeTestDefinition],
        invocations: list[RuntimeTestInvocation],
        analysis: SnapshotAnalysis | None,
    ) -> None:
        assert analysis is not None
        store.persist_runtime_observations(
            analysis, environment, execution, definitions, invocations
        )

    runtime_actions = RuntimeActionService(
        executor,
        persist_result=persist_runtime,
    )

    def collect(analysis: SnapshotAnalysis):
        assert analysis.runtime_materialization_id
        snapshot_root = runtime_files_root(
            settings.snapshot_cache_dir,
            analysis.snapshot_id,
            analysis.runtime_materialization_id,
        )
        return runtime_actions.collect(
            analysis.snapshot_id,
            analysis.runtime_materialization_id,
            snapshot_root,
            executor.ensure_available(),
            analysis=analysis,
        )

    service = CommitDiffService(
        settings,
        IngestionJobManager(),
        collection_provider=collect,
    )
    result = service.analyze(
        DiffAnalysisRequest(repository_path=str(repo), base_sha=base_sha, target_sha=target_sha)
    )

    assert store.snapshot_status(result.base_snapshot_id) == "READY"
    assert store.snapshot_status(result.target_snapshot_id) == "READY"
    assert result.base_snapshot_id != result.target_snapshot_id
    assert result.target_commit_sha == target_sha
    target_analysis = load_snapshot_analysis(result.target_snapshot_id, settings).model_copy(
        update={"status": SnapshotStatus.READY}
    )
    assert target_analysis.runtime_materialization_id
    materialized_root = runtime_files_root(
        settings.snapshot_cache_dir,
        result.target_snapshot_id,
        target_analysis.runtime_materialization_id,
    )
    assert not (materialized_root / "uncommitted-local.py").exists()
    assert result.test_plan.collection_status == "COMPLETED"
    assert any(
        recommendation.pytest_node_id == "tests/test_module.py::test_changed"
        and recommendation.tier == "C"
        and recommendation.test_invocation_id
        and any(
            evidence.evidence_type == "TEST_COLLECTION"
            and evidence.runtime_environment_id
            and evidence.record_id.startswith("run_")
            for evidence in recommendation.evidence
        )
        for recommendation in result.test_plan.recommendations
    )

    recommendation = next(
        item
        for item in result.test_plan.recommendations
        if item.pytest_node_id == "tests/test_module.py::test_changed"
    )
    assert recommendation.test_invocation_id
    assert result.test_plan.runtime_environment_id
    prepared = runtime_actions.prepare(
        result.target_snapshot_id,
        result.test_plan.runtime_environment_id,
        [recommendation.test_invocation_id],
    )
    assert prepared.snapshot_id == result.target_snapshot_id
    assert prepared.runtime_environment_id == result.test_plan.runtime_environment_id
    assert prepared.pytest_command[-1] == "tests/test_module.py::test_changed"
    approved = runtime_actions.approve(prepared.approval_id)
    assert approved.status.value == "APPROVED"
    execution = runtime_actions.execute(approved.approval_id)
    assert execution.status == RuntimeRunStatus.COMPLETED
    assert execution.passed == 1 and execution.failed == 0
    assert execution.snapshot_id == result.target_snapshot_id
    assert execution.runtime_environment_id == result.test_plan.runtime_environment_id
    assert execution.run_id == prepared.run_id

    runtime_rows = store.runtime_tool_records(result.target_snapshot_id)
    assert runtime_rows
    assert all(row["snapshot_id"] == result.target_snapshot_id for row in runtime_rows)
    assert all(
        row["runtime_environment_id"] == result.test_plan.runtime_environment_id
        for row in runtime_rows
    )
    assert any(row.get("run_id") == execution.run_id for row in runtime_rows)
    runtime_tools = DeterministicGraphTools.from_store(target_analysis, store)
    changed_entity = next(
        entity for entity in target_analysis.entities if entity.qualified_name.endswith("changed")
    )
    observed = runtime_tools.find_observed_tests(
        GraphToolRequest(snapshot_id=result.target_snapshot_id),
        entity_ids=(changed_entity.entity_id,),
    )
    assert any(
        item.get("run_id") == execution.run_id
        and item.get("test_invocation_id") == recommendation.test_invocation_id
        for item in observed.results
    )
    query = asyncio.run(
        QueryOrchestrator(runtime_tools).execute(
            "Which tests observed changed?",
            snapshot_id=result.target_snapshot_id,
            selected_node_id=changed_entity.entity_id,
        )
    )
    refreshed = build_investigation_response(query, runtime_tools)
    assert refreshed.snapshot_id == result.target_snapshot_id
    assert any(
        item.result.tool == "find_observed_tests" and item.result.results
        for item in query.tool_executions
    )


def test_falkordb_compare_and_swap_preserves_concurrent_investigation_notes(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "investigation-cas")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )
    analysis = analyze_repository(repo, settings)
    store = GraphStore(settings)
    store.publish_snapshot(analysis)
    ready = analysis.model_copy(update={"status": SnapshotStatus.READY})
    tools = DeterministicGraphTools.from_store(ready, store)
    query = asyncio.run(
        QueryOrchestrator(tools).execute(
            "Who calls `pkg.auth.verify_token`?",
            snapshot_id=analysis.snapshot_id,
        )
    )
    response = build_investigation_response(query, tools)
    service_one = InvestigationMemoryService(GraphStore(settings))
    created = service_one.save_result(
        response,
        question="Who calls `pkg.auth.verify_token`?",
    )
    barrier = threading.Barrier(2)
    barrier_used = threading.local()

    class FirstReadBarrierStore:
        def __init__(self, backing: GraphStore) -> None:
            self.backing = backing

        def __getattr__(self, name: str) -> Any:
            return getattr(self.backing, name)

        def get_investigation_memory(self, investigation_id: str) -> str | None:
            value = self.backing.get_investigation_memory(investigation_id)
            if not getattr(barrier_used, "used", False):
                barrier_used.used = True
                barrier.wait(timeout=10)
            return value

    service_two = InvestigationMemoryService(FirstReadBarrierStore(GraphStore(settings)))
    service_three = InvestigationMemoryService(FirstReadBarrierStore(GraphStore(settings)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                service_two.add_note,
                created.investigation_id,
                snapshot_id=analysis.snapshot_id,
                text="concurrent note one",
            ),
            pool.submit(
                service_three.add_note,
                created.investigation_id,
                snapshot_id=analysis.snapshot_id,
                text="concurrent note two",
            ),
        ]
        [future.result(timeout=20) for future in futures]

    reopened = InvestigationMemoryService(GraphStore(settings)).get(created.investigation_id)
    assert {item.text for item in reopened.user_notes} == {
        "concurrent note one",
        "concurrent note two",
    }
    assert reopened.snapshot_id == analysis.snapshot_id
    assert reopened.repository_id == analysis.repository.repository_id
    assert reopened.record_revision == created.record_revision + 2

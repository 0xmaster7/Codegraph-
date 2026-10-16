import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .claims import ClaimValidator, DraftAnswer, ValidatedAnswer
from .config import get_settings
from .diff_analysis import (
    CommitDiffService,
    DiffAnalysisRequest,
    DiffAnalysisResponse,
    M21DiffError,
)
from .graph import GraphStore
from .graph_tools import DeterministicGraphTools, GraphToolRequest
from .ingestion import (
    RepositoryConfigurationError,
    RepositoryError,
    analyze_repository,
    inspect_repository,
    load_snapshot_analysis,
)
from .investigation import InvestigationResponse, build_investigation_response
from .investigation_memory import (
    InvestigationMemoryRecord,
    InvestigationMemoryService,
    InvestigationMemorySummary,
    InvestigationSelection,
    InvestigationSnapshotMismatch,
)
from .jobs import IngestionJobManager, JobCancellationRejected
from .local_query_analyzer import OllamaQueryAnalyzerProvider
from .materialization import RuntimeMaterializationError, runtime_files_root
from .models import (
    ActionApproval,
    IngestionJob,
    ReplayOutcome,
    RuntimeCollection,
    RuntimeEnvironment,
    RuntimeValidationReport,
    SnapshotAnalysis,
    SnapshotStatus,
    SourceSite,
    TestDefinition,
    TestExecutionResult,
    TestInvocation,
)
from .orchestrator import LLMQueryAnalyzer, QueryOrchestrator, QueryResponse
from .planner import QueryPlan, analyze_query
from .provenance import failed_closed_edge_ids, replay_edge
from .retrieval import SymbolResolution, get_source_span, resolve_symbol
from .test_execution import (
    ApprovalError,
    DockerTestExecutor,
    DockerUnavailable,
    RuntimeActionService,
    RuntimeExecutionError,
)
from .tool_services import load_snapshot_tool_context

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime_actions.executor.cleanup_orphaned_containers()
    try:
        interrupted = ingestion_jobs.recover_interrupted_jobs(settings)
        if interrupted:
            logger.info("Marked %s interrupted ingestion jobs after restart", len(interrupted))
        store = GraphStore(settings)
        store.recover_interrupted_runtime_persistence()
        store.garbage_collect_snapshots()
    except Exception:
        logger.warning(
            "Could not recover jobs or collect snapshots because FalkorDB is unavailable"
        )
    yield


app = FastAPI(title="FalkorDB CodeGraph", version="0.1.0", lifespan=lifespan)
ingestion_jobs = IngestionJobManager()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
settings = get_settings()


def _query_orchestrator(tools: DeterministicGraphTools, *, tool_limit: int) -> QueryOrchestrator:
    analyzer = None
    if settings.local_query_analyzer_enabled:
        provider = OllamaQueryAnalyzerProvider(
            settings.local_query_analyzer_url,
            settings.local_query_analyzer_model,
        )
        analyzer = LLMQueryAnalyzer(provider)
    return QueryOrchestrator(tools, analyzer, tool_limit=tool_limit)


def _persist_runtime_result(
    environment: RuntimeEnvironment,
    result: TestExecutionResult,
    definitions: list[TestDefinition],
    invocations: list[TestInvocation],
    analysis: SnapshotAnalysis | None,
) -> None:
    if analysis is None:
        raise RuntimeError("Runtime observations require the immutable snapshot analysis")
    GraphStore(settings).persist_runtime_observations(
        analysis, environment, result, definitions, invocations
    )


runtime_actions = RuntimeActionService(
    DockerTestExecutor(settings), persist_result=_persist_runtime_result
)


def _collect_snapshot_tests(analysis: SnapshotAnalysis) -> RuntimeCollection:
    if not analysis.runtime_materialization_id:
        raise RuntimeMaterializationError(
            "The committed snapshot has no immutable runtime materialization for test collection"
        )
    image = runtime_actions.executor.ensure_available()
    snapshot_root = runtime_files_root(
        settings.snapshot_cache_dir,
        analysis.snapshot_id,
        analysis.runtime_materialization_id,
    )
    return runtime_actions.collect(
        analysis.snapshot_id,
        analysis.runtime_materialization_id,
        snapshot_root,
        image,
        analysis=analysis,
    )


commit_diff_service = CommitDiffService(
    settings,
    ingestion_jobs,
    collection_provider=_collect_snapshot_tests,
)


class RepositoryRequest(BaseModel):
    path: str


class SnapshotRequest(RepositoryRequest):
    model_config = ConfigDict(extra="forbid")

    include_untracked: bool = False


class SnapshotPinRequest(BaseModel):
    pinned: bool = True


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4096)
    selected_node_id: str | None = Field(default=None, max_length=160)
    selected_edge_id: str | None = Field(default=None, max_length=160)
    selected_site_id: str | None = Field(default=None, max_length=160)


class QueryExecutionRequest(QueryRequest):
    snapshot_id: str = Field(min_length=1, max_length=128)
    result_limit: int = Field(default=50, ge=1, le=100)
    investigation_id: str | None = Field(default=None, pattern=r"^investigation_[0-9a-f]{32}$")


class InvestigationNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class QueryValidationRequest(QueryExecutionRequest):
    draft_answer: DraftAnswer


class EvidenceReplayRequest(BaseModel):
    snapshot_id: str


class RuntimeCollectionRequest(BaseModel):
    snapshot_id: str


class RuntimeActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=128)
    runtime_environment_id: str = Field(min_length=1, max_length=160)
    test_ids: list[str] = Field(min_length=1, max_length=500)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        GraphStore(settings).ping()
        return {"status": "ok", "falkordb": "connected"}
    except Exception:
        return {"status": "ok", "falkordb": "unavailable"}


@app.post("/api/repos/inspect")
def repository_inspect(request: RepositoryRequest) -> dict[str, object]:
    try:
        _, info = inspect_repository(request.path, settings)
        return info.model_dump(mode="json")
    except RepositoryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RepositoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/repos/register")
def repository_register(request: RepositoryRequest) -> dict[str, object]:
    try:
        _, info = inspect_repository(request.path, settings)
        GraphStore(settings).register_repository(info)
        return info.model_dump(mode="json")
    except RepositoryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RepositoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="FalkorDB repository registration failed"
        ) from exc


@app.post("/api/query/plan", response_model=QueryPlan)
def query_plan(request: QueryRequest) -> QueryPlan:
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="Question cannot be empty")
    return analyze_query(
        request.question,
        snapshot_id=None,
        selected_node_id=request.selected_node_id,
        selected_edge_id=request.selected_edge_id,
        selected_site_id=request.selected_site_id,
    )


@app.post("/api/query", response_model=QueryResponse)
async def query_execute(request: QueryExecutionRequest) -> QueryResponse:
    try:
        store = GraphStore(settings)
        status = store.snapshot_status(request.snapshot_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if status != "READY":
            raise HTTPException(status_code=409, detail="Queries require a READY snapshot")
        analysis = load_snapshot_analysis(request.snapshot_id, settings).model_copy(
            update={"status": SnapshotStatus.READY}
        )
        tools = DeterministicGraphTools.from_store(analysis, store)
        orchestrator = _query_orchestrator(tools, tool_limit=request.result_limit)
        return await orchestrator.execute(
            request.question,
            snapshot_id=request.snapshot_id,
            selected_node_id=request.selected_node_id,
            selected_edge_id=request.selected_edge_id,
            selected_site_id=request.selected_site_id,
        )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Query execution failed") from exc


@app.post("/api/query/investigate", response_model=InvestigationResponse)
async def query_investigate(request: QueryExecutionRequest) -> InvestigationResponse:
    """Run M18/M19 and persist the snapshot-bound investigation memory."""
    try:
        store = GraphStore(settings)
        status = store.snapshot_status(request.snapshot_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if status != "READY":
            raise HTTPException(status_code=409, detail="Queries require a READY snapshot")
        if request.investigation_id is not None:
            InvestigationMemoryService(store).get(
                request.investigation_id, snapshot_id=request.snapshot_id
            )
        analysis = load_snapshot_analysis(request.snapshot_id, settings).model_copy(
            update={"status": SnapshotStatus.READY}
        )
        tools = DeterministicGraphTools.from_store(analysis, store)
        query = await _query_orchestrator(tools, tool_limit=request.result_limit).execute(
            request.question,
            snapshot_id=request.snapshot_id,
            selected_node_id=request.selected_node_id,
            selected_edge_id=request.selected_edge_id,
            selected_site_id=request.selected_site_id,
        )
        response = build_investigation_response(query, tools)
        selected = tuple(
            item
            for item in (
                InvestigationSelection(kind="entity", selected_id=request.selected_node_id)
                if request.selected_node_id
                else None,
                InvestigationSelection(kind="site", selected_id=request.selected_site_id)
                if request.selected_site_id
                else None,
                InvestigationSelection(kind="edge", selected_id=request.selected_edge_id)
                if request.selected_edge_id
                else None,
            )
            if item is not None
        )
        memory = InvestigationMemoryService(store).save_result(
            response,
            question=request.question,
            selected_entities=selected,
            investigation_id=request.investigation_id,
        )
        return memory.latest_result
    except HTTPException:
        raise
    except InvestigationSnapshotMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SNAPSHOT_MISMATCH",
                "investigation_id": exc.investigation_id,
                "stored_snapshot_id": exc.stored_snapshot_id,
                "requested_snapshot_id": exc.requested_snapshot_id,
            },
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Investigation query failed") from exc


@app.get("/api/investigations", response_model=list[InvestigationMemorySummary])
def investigations_list(
    snapshot_id: str,
    limit: int = 20,
) -> list[InvestigationMemorySummary]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100")
    try:
        store = GraphStore(settings)
        status = store.snapshot_status(snapshot_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if status != SnapshotStatus.READY.value:
            raise HTTPException(
                status_code=409, detail="Investigation listing requires a READY snapshot"
            )
        return InvestigationMemoryService(store).list(snapshot_id, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Investigation memory query failed") from exc


@app.get("/api/investigations/{investigation_id}", response_model=InvestigationMemoryRecord)
def investigation_get(
    investigation_id: str,
    snapshot_id: str | None = None,
) -> InvestigationMemoryRecord:
    try:
        return InvestigationMemoryService(GraphStore(settings)).get(
            investigation_id, snapshot_id=snapshot_id
        )
    except InvestigationSnapshotMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SNAPSHOT_MISMATCH",
                "investigation_id": exc.investigation_id,
                "stored_snapshot_id": exc.stored_snapshot_id,
                "requested_snapshot_id": exc.requested_snapshot_id,
            },
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Investigation memory query failed") from exc


@app.post(
    "/api/investigations/{investigation_id}/notes",
    response_model=InvestigationMemoryRecord,
)
def investigation_add_note(
    investigation_id: str,
    request: InvestigationNoteRequest,
) -> InvestigationMemoryRecord:
    try:
        return InvestigationMemoryService(GraphStore(settings)).add_note(
            investigation_id, snapshot_id=request.snapshot_id, text=request.text
        )
    except InvestigationSnapshotMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SNAPSHOT_MISMATCH",
                "investigation_id": exc.investigation_id,
                "stored_snapshot_id": exc.stored_snapshot_id,
                "requested_snapshot_id": exc.requested_snapshot_id,
            },
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Investigation note could not be saved"
        ) from exc


@app.post("/api/query/validate", response_model=ValidatedAnswer)
async def query_validate(request: QueryValidationRequest) -> ValidatedAnswer:
    """Execute a snapshot query, then validate its untrusted structured answer draft."""
    try:
        store = GraphStore(settings)
        status = store.snapshot_status(request.snapshot_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if status != "READY":
            raise HTTPException(status_code=409, detail="Queries require a READY snapshot")
        analysis = load_snapshot_analysis(request.snapshot_id, settings).model_copy(
            update={"status": SnapshotStatus.READY}
        )
        tools = DeterministicGraphTools.from_store(analysis, store)
        response = await _query_orchestrator(tools, tool_limit=request.result_limit).execute(
            request.question,
            snapshot_id=request.snapshot_id,
            selected_node_id=request.selected_node_id,
            selected_edge_id=request.selected_edge_id,
            selected_site_id=request.selected_site_id,
        )
        return ClaimValidator(tools).validate(
            request.draft_answer,
            evidence_pool=response.evidence_pool,
            executions=response.tool_executions,
            diagnostics=response.diagnostics,
        )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Query validation failed") from exc


@app.post("/api/snapshots/inspect", response_model=SnapshotAnalysis)
def snapshot_inspect(request: SnapshotRequest) -> SnapshotAnalysis:
    try:
        return analyze_repository(request.path, settings, request.include_untracked)
    except RepositoryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RepositoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/diff/analyze", response_model=DiffAnalysisResponse)
def diff_analyze(request: DiffAnalysisRequest) -> DiffAnalysisResponse:
    """Compare two immutable commits and propose evidence-tiered tests."""
    try:
        return commit_diff_service.analyze(request)
    except RepositoryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except M21DiffError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Required snapshot analysis is unavailable"
        ) from exc
    except Exception as exc:
        logger.warning("M21 commit diff analysis failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Commit diff analysis could not complete"
        ) from exc


@app.post("/api/snapshots/ingest", response_model=IngestionJob, status_code=202)
def snapshot_ingest(request: SnapshotRequest) -> IngestionJob:
    try:
        return ingestion_jobs.submit(request.path, settings, request.include_untracked)
    except RepositoryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RepositoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Snapshot ingestion could not be scheduled"
        ) from exc


@app.post("/api/snapshots/{snapshot_id}/cancel", response_model=IngestionJob)
def cancel_snapshot_ingestion(snapshot_id: str) -> IngestionJob:
    try:
        return ingestion_jobs.cancel(snapshot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobCancellationRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/snapshots/{snapshot_id}")
def snapshot_status(snapshot_id: str) -> dict[str, object]:
    try:
        graph = GraphStore(settings).metadata_graph()
        result = graph.query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) RETURN s",
            {"snapshot_id": snapshot_id},
        )
        if not result.result_set:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return {"snapshot": result.result_set[0][0].properties}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB unavailable") from exc


@app.post("/api/runtime/baseline/plan", response_model=RuntimeCollection)
def runtime_baseline_plan(request: RuntimeCollectionRequest) -> RuntimeCollection:
    try:
        if GraphStore(settings).snapshot_status(request.snapshot_id) != "READY":
            raise HTTPException(
                status_code=409, detail="Runtime collection requires a READY snapshot"
            )
        analysis = load_snapshot_analysis(request.snapshot_id, settings)
        materialization_id = analysis.runtime_materialization_id
        if not materialization_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This snapshot has no immutable runtime files; "
                    "ingest it again before test collection."
                ),
            )
        image = runtime_actions.executor.ensure_available()
        snapshot_root = runtime_files_root(
            settings.snapshot_cache_dir, request.snapshot_id, materialization_id
        )
        return runtime_actions.collect(
            request.snapshot_id, materialization_id, snapshot_root, image, analysis=analysis
        )
    except HTTPException:
        raise
    except DockerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeMaterializationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/runtime/action/prepare", response_model=ActionApproval)
def runtime_action_prepare(request: RuntimeActionRequest) -> ActionApproval:
    try:
        return runtime_actions.prepare(
            request.snapshot_id, request.runtime_environment_id, request.test_ids
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runtime/action/{approval_id}/approve", response_model=ActionApproval)
def runtime_action_approve(approval_id: str) -> ActionApproval:
    try:
        return runtime_actions.approve(approval_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runtime/action/{approval_id}", response_model=ActionApproval)
def runtime_action_state(approval_id: str) -> ActionApproval:
    try:
        return runtime_actions.approval_state(approval_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runtime/action/{approval_id}/revoke", response_model=ActionApproval)
def runtime_action_revoke(approval_id: str) -> ActionApproval:
    try:
        return runtime_actions.revoke(approval_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runtime/action/{approval_id}/execute", response_model=TestExecutionResult)
def runtime_action_execute(approval_id: str) -> TestExecutionResult:
    try:
        return runtime_actions.execute(approval_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DockerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeMaterializationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/runtime/runs/{run_id}/cancel")
def runtime_run_cancel(run_id: str) -> dict[str, object]:
    if not runtime_actions.cancel(run_id):
        raise HTTPException(status_code=404, detail="No active runtime run with this ID")
    return {"run_id": run_id, "cancel_requested": True}


@app.get("/api/runtime/runs/{run_id}", response_model=TestExecutionResult)
def runtime_run_get(run_id: str) -> TestExecutionResult:
    result = runtime_actions.get_result(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Runtime run result not found")
    return result


@app.get("/api/runtime/runs/{run_id}/validation", response_model=RuntimeValidationReport)
def runtime_run_validation(run_id: str) -> RuntimeValidationReport:
    result = runtime_actions.get_result(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Runtime run result not found")
    if result.runtime_validation is None:
        raise HTTPException(status_code=409, detail="Runtime validation is not available")
    return result.runtime_validation


@app.get("/api/snapshots")
def snapshots_list(limit: int = 50) -> dict[str, object]:
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 200")
    try:
        return {"snapshots": GraphStore(settings).list_snapshots(limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB unavailable") from exc


@app.get("/api/snapshots/{snapshot_id}/analysis", response_model=SnapshotAnalysis)
def snapshot_analysis(snapshot_id: str) -> SnapshotAnalysis:
    try:
        return load_snapshot_analysis(snapshot_id, settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/snapshots/{snapshot_id}/pin")
def snapshot_pin(snapshot_id: str, request: SnapshotPinRequest) -> dict[str, object]:
    try:
        GraphStore(settings).set_snapshot_pinned(snapshot_id, request.pinned)
        return {"snapshot_id": snapshot_id, "pinned": request.pinned}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB snapshot pin update failed") from exc


@app.get("/api/snapshots/{snapshot_id}/symbols/resolve", response_model=SymbolResolution)
def symbol_resolve(snapshot_id: str, query: str, limit: int = 100) -> SymbolResolution:
    if not query.strip():
        raise HTTPException(status_code=422, detail="Symbol query cannot be empty")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 500")
    try:
        context = load_snapshot_tool_context(snapshot_id, settings=settings)
        return resolve_symbol(context.analysis, query, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/snapshots/{snapshot_id}/sites/{site_id}", response_model=SourceSite)
def site_detail(snapshot_id: str, site_id: str) -> SourceSite:
    try:
        analysis = load_snapshot_analysis(snapshot_id, settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    site = next((item for item in analysis.sites if item.site_id == site_id), None)
    if site is None:
        raise HTTPException(status_code=404, detail="Source site not found")
    return site


@app.get("/api/snapshots/{snapshot_id}/source")
def source_detail(
    snapshot_id: str,
    file_path: str,
    start_byte: int,
    end_byte: int,
) -> dict[str, object]:
    try:
        analysis = load_snapshot_analysis(snapshot_id, settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
        raise HTTPException(status_code=400, detail="Invalid repository-relative file path")
    entry = next((item for item in analysis.manifest if item.path == file_path), None)
    if entry is None or not entry.analyzed:
        raise HTTPException(status_code=404, detail="Analyzed source file not found")
    if len(entry.blob_hash) != 64 or any(
        char not in "0123456789abcdef" for char in entry.blob_hash
    ):
        raise HTTPException(status_code=409, detail="Invalid snapshot blob identity")
    blob = (
        settings.snapshot_cache_dir.expanduser().resolve() / snapshot_id / "blobs" / entry.blob_hash
    )
    try:
        content = blob.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot source blob is missing") from exc
    if hashlib.sha256(content).hexdigest() != entry.content_hash:
        raise HTTPException(status_code=409, detail="Snapshot source hash mismatch")
    try:
        result = get_source_span(analysis, file_path, start_byte, end_byte, content)
    except ValueError as exc:
        raise HTTPException(status_code=416, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@app.post("/api/evidence/{evidence_id}/replay")
def evidence_replay(evidence_id: str, request: EvidenceReplayRequest) -> dict[str, object]:
    try:
        analysis = load_snapshot_analysis(request.snapshot_id, settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = replay_edge(analysis, evidence_id, settings)
    matching = next(
        (
            record
            for record in analysis.provenance
            if record.edge_id == evidence_id or record.provenance_id == evidence_id
        ),
        None,
    )
    if matching is not None:
        # A transient/inconclusive retry cannot erase a previously established integrity
        # mismatch. Only an authoritative PASS, or another fail-closed result, may replace it.
        prior_fail_closed = any(
            record.edge_id == matching.edge_id
            and record.replay_state
            in {ReplayOutcome.EDGE_NOT_REPRODUCED, ReplayOutcome.SOURCE_HASH_MISMATCH}
            for record in analysis.provenance
        )
        if prior_fail_closed and result.outcome not in {
            ReplayOutcome.PASS,
            ReplayOutcome.EDGE_NOT_REPRODUCED,
            ReplayOutcome.SOURCE_HASH_MISMATCH,
        }:
            return result.model_dump(mode="json")
        updated_record = matching.model_copy(
            update={"replay_state": result.outcome, "review_state": result.review_state}
        )
        provenance = [
            updated_record if record.provenance_id == updated_record.provenance_id else record
            for record in analysis.provenance
        ]
        updated = analysis.model_copy(update={"provenance": provenance})
        cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
        analysis_path = cache_root / "analysis.json"
        temporary = cache_root / f"analysis.{os.urandom(8).hex()}.tmp"
        temporary.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, analysis_path)
        try:
            GraphStore(settings).update_provenance_replay(
                request.snapshot_id, updated_record.provenance_id, updated_record
            )
        except KeyError:
            pass
        except Exception:
            logger.warning("Could not update replay state in FalkorDB", exc_info=True)
    return result.model_dump(mode="json")


@app.get("/api/snapshots/{snapshot_id}/graph/symbols/resolve")
def graph_symbol_resolve(snapshot_id: str, query: str, limit: int = 20) -> dict[str, object]:
    if not query.strip():
        raise HTTPException(status_code=422, detail="Symbol query cannot be empty")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 500")
    try:
        context = load_snapshot_tool_context(snapshot_id, settings=settings)
        result = resolve_symbol(context.analysis, query.strip(), limit=limit)
        return {**result.model_dump(mode="json"), "snapshot_status": "READY"}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB query failed") from exc


@app.get("/api/snapshots/{snapshot_id}/graph/callers")
def graph_callers(
    snapshot_id: str,
    target_id: str,
    limit: int = 100,
) -> dict[str, object]:
    if not target_id.strip():
        raise HTTPException(status_code=422, detail="Target ID cannot be empty")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 500")
    try:
        context = load_snapshot_tool_context(snapshot_id, settings=settings)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return context.tools.find_callers(request, target_id.strip()).model_dump(mode="json")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB query failed") from exc


@app.get("/api/snapshots/{snapshot_id}/graph/references")
def graph_references(
    snapshot_id: str,
    target_id: str,
    limit: int = 100,
) -> dict[str, object]:
    if not target_id.strip():
        raise HTTPException(status_code=422, detail="Target ID cannot be empty")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 500")
    try:
        context = load_snapshot_tool_context(snapshot_id, settings=settings)
        request = GraphToolRequest(snapshot_id=snapshot_id, limit=limit)
        return context.tools.find_references(request, target_id.strip()).model_dump(mode="json")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot analysis not found") from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB query failed") from exc


@app.get("/api/snapshots/{snapshot_id}/graph/callees")
def graph_callees(
    snapshot_id: str,
    site_id: str,
    limit: int = 100,
) -> dict[str, object]:
    if not site_id.strip():
        raise HTTPException(status_code=422, detail="Call site ID cannot be empty")
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 500")
    try:
        analysis = load_snapshot_analysis(snapshot_id, settings)
        return GraphStore(settings).find_callees(
            snapshot_id,
            site_id.strip(),
            limit,
            excluded_edge_ids=tuple(sorted(failed_closed_edge_ids(analysis))),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB query failed") from exc

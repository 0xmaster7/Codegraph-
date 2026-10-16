from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .graph import GraphStore
from .ids import stable_id
from .ingestion import (
    AnalysisCancelled,
    analyze_repository,
    identify_snapshot,
    inspect_repository,
    load_snapshot_analysis,
)
from .lifecycle import register_active_snapshot, snapshot_lifecycle_lock, unregister_active_snapshot
from .materialization import capture_runtime_materialization
from .models import IngestionJob, IngestionJobStatus, RepositoryInfo, SnapshotAnalysis

logger = logging.getLogger(__name__)


class JobCancellationRejected(ValueError):
    pass


@dataclass
class _ActiveJob:
    response: IngestionJob
    repository: RepositoryInfo
    path: str
    settings: Settings
    include_untracked: bool
    repository_id_override: str | None
    skip_worktree_status: bool
    runtime_materialization_id: str
    cancellation_event: threading.Event
    future: Future[None] | None = None


class IngestionJobManager:
    """Single-process coordinator for cancellable local ingestion jobs."""

    _ACTIVE_STATUSES = {
        IngestionJobStatus.QUEUED,
        IngestionJobStatus.MATERIALIZING,
        IngestionJobStatus.PARSING,
        IngestionJobStatus.SEMANTIC_INDEXING,
        IngestionJobStatus.VALIDATING,
        IngestionJobStatus.PUBLISHING,
    }

    def __init__(
        self,
        *,
        store_factory: Callable[[Settings], GraphStore] = GraphStore,
        processor: Callable[..., SnapshotAnalysis] = analyze_repository,
    ) -> None:
        self._store_factory = store_factory
        self._processor = processor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codegraph")
        self._lock = threading.RLock()
        self._jobs: dict[str, _ActiveJob] = {}
        self._recovery_complete = False

    def recover_interrupted_jobs(self, settings: Settings) -> list[str]:
        with self._lock:
            if self._recovery_complete:
                return []
            recovered = self._store_factory(settings).recover_interrupted_ingestion_jobs()
            self._recovery_complete = True
            return recovered

    def submit(
        self,
        path: str | Path,
        settings: Settings,
        include_untracked: bool = False,
        *,
        canonical_repository_root: str | None = None,
        repository_id_override: str | None = None,
        skip_worktree_status: bool = False,
    ) -> IngestionJob:
        snapshot_id, repository = identify_snapshot(
            path,
            settings,
            include_untracked,
            repository_id_override=repository_id_override,
            skip_worktree_status=skip_worktree_status,
        )
        materialization_root = Path(repository.root)
        if canonical_repository_root is not None:
            canonical_root, canonical_repository = inspect_repository(
                canonical_repository_root,
                settings,
                repository_id_override=repository_id_override,
                skip_worktree_status=True,
            )
            if canonical_repository.repository_id != repository.repository_id:
                raise ValueError("Commit worktree belongs to a different repository")
            repository = repository.model_copy(update={"root": str(canonical_root)})
        self.recover_interrupted_jobs(settings)
        with self._lock, snapshot_lifecycle_lock():
            active = self._jobs.get(snapshot_id)
            if active and active.response.status in self._ACTIVE_STATUSES:
                return active.response.model_copy(update={"attached": True})

            store = self._store_factory(settings)
            store.garbage_collect_snapshots()
            materialization = capture_runtime_materialization(
                materialization_root,
                settings.snapshot_cache_dir,
                snapshot_id,
                include_untracked=include_untracked,
            )
            if store.snapshot_status(snapshot_id) == "READY":
                analysis = load_snapshot_analysis(snapshot_id, settings)
                if analysis.runtime_materialization_id != materialization.materialization_id:
                    updated = analysis.model_copy(
                        update={"runtime_materialization_id": materialization.materialization_id}
                    )
                    analysis_path = (
                        settings.snapshot_cache_dir.expanduser().resolve()
                        / snapshot_id
                        / "analysis.json"
                    )
                    temporary = analysis_path.with_name(f"analysis.{uuid.uuid4().hex}.tmp")
                    temporary.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
                    temporary.replace(analysis_path)
                    store.update_runtime_materialization(
                        snapshot_id, materialization.materialization_id
                    )
                return IngestionJob(
                    job_id=stable_id("existing", snapshot_id, prefix="job_"),
                    snapshot_id=snapshot_id,
                    status=IngestionJobStatus.COMPLETED,
                    attached=True,
                )

            job = IngestionJob(
                job_id=stable_id(snapshot_id, uuid.uuid4().hex, prefix="job_"),
                snapshot_id=snapshot_id,
                status=IngestionJobStatus.QUEUED,
            )
            record = _ActiveJob(
                response=job,
                repository=repository,
                path=str(Path(path).expanduser().resolve()),
                settings=settings,
                include_untracked=include_untracked,
                repository_id_override=repository_id_override,
                skip_worktree_status=skip_worktree_status,
                runtime_materialization_id=materialization.materialization_id,
                cancellation_event=threading.Event(),
            )
            store.update_ingestion_job(job, repository)
            register_active_snapshot(snapshot_id)
            self._jobs[snapshot_id] = record
            try:
                record.future = self._executor.submit(self._run, record)
            except Exception:
                unregister_active_snapshot(snapshot_id)
                self._jobs.pop(snapshot_id, None)
                raise
            return job

    def cancel(self, snapshot_id: str) -> IngestionJob:
        with self._lock:
            record = self._jobs.get(snapshot_id)
            if record is None:
                raise KeyError("No active ingestion job for this snapshot")
            if record.response.status not in self._ACTIVE_STATUSES:
                raise JobCancellationRejected("Ingestion job is already terminal")
            if record.response.status == IngestionJobStatus.PUBLISHING:
                raise JobCancellationRejected("Snapshot publication has already started")
            record.cancellation_event.set()
            record.response = record.response.model_copy(update={"cancel_requested": True})
            return record.response

    def get(self, snapshot_id: str) -> IngestionJob | None:
        with self._lock:
            record = self._jobs.get(snapshot_id)
            return record.response.model_copy() if record else None

    def wait(self, snapshot_id: str, timeout: float | None = None) -> IngestionJob:
        with self._lock:
            record = self._jobs.get(snapshot_id)
            if record is None:
                raise KeyError("Ingestion job not found")
            future = record.future
        if future:
            future.result(timeout=timeout)
        return self.get(snapshot_id) or record.response

    def _set_status(
        self,
        record: _ActiveJob,
        status: IngestionJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        with self._lock:
            record.response = record.response.model_copy(
                update={"status": status, "cancel_requested": False, "error": error}
            )
            response = record.response
        self._store_factory(record.settings).update_ingestion_job(response, record.repository)

    def _run(self, record: _ActiveJob) -> None:
        try:
            if record.cancellation_event.is_set():
                raise AnalysisCancelled("Ingestion was cancelled")
            self._set_status(record, IngestionJobStatus.MATERIALIZING)
            processor_options: dict[str, object] = {}
            if record.repository_id_override is not None:
                processor_options["repository_id_override"] = record.repository_id_override
            if record.skip_worktree_status:
                processor_options["skip_worktree_status"] = True
            analysis = self._processor(
                record.path,
                record.settings,
                record.include_untracked,
                cancellation_event=record.cancellation_event,
                on_stage=lambda stage: self._set_status(record, IngestionJobStatus(stage)),
                expected_snapshot_id=record.response.snapshot_id,
                expected_runtime_materialization_id=record.runtime_materialization_id,
                **processor_options,
            )
            if (
                isinstance(analysis, SnapshotAnalysis)
                and analysis.repository.root != record.repository.root
            ):
                if analysis.repository.repository_id != record.repository.repository_id:
                    raise ValueError("Ingestion repository identity changed before publication")
                analysis = analysis.model_copy(
                    update={
                        "repository": analysis.repository.model_copy(
                            update={"root": record.repository.root}
                        )
                    }
                )
            if record.cancellation_event.is_set():
                raise AnalysisCancelled("Ingestion was cancelled")
            self._set_status(record, IngestionJobStatus.VALIDATING)
            with self._lock:
                if record.cancellation_event.is_set():
                    raise AnalysisCancelled("Ingestion was cancelled")
                record.response = record.response.model_copy(
                    update={"status": IngestionJobStatus.PUBLISHING, "cancel_requested": False}
                )
                publishing = record.response
            store = self._store_factory(record.settings)
            store.update_ingestion_job(publishing, record.repository)
            store.publish_snapshot(analysis, job_id=publishing.job_id)
            with self._lock:
                record.response = record.response.model_copy(
                    update={
                        "status": IngestionJobStatus.COMPLETED,
                        "cancel_requested": False,
                        "error": None,
                    }
                )
        except AnalysisCancelled:
            self._set_status(record, IngestionJobStatus.CANCELLED)
        except Exception as exc:
            self._set_status(record, IngestionJobStatus.FAILED, error=type(exc).__name__)
        finally:
            unregister_active_snapshot(record.response.snapshot_id)
            if record.response.status in {
                IngestionJobStatus.COMPLETED,
                IngestionJobStatus.FAILED,
                IngestionJobStatus.CANCELLED,
            }:
                try:
                    self._store_factory(record.settings).garbage_collect_snapshots()
                except Exception:
                    logger.warning("Snapshot GC did not finish after ingestion", exc_info=True)

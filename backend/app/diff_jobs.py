"""Bounded background comparisons so HTTP proxies only carry short polling requests."""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .diff_analysis import DiffAnalysisRequest, DiffAnalysisResponse, M21DiffError

logger = logging.getLogger(__name__)


class ComparisonBusy(RuntimeError):
    pass


@dataclass
class _Job:
    created_at: float
    future: Future[DiffAnalysisResponse]


class DiffJobManager:
    def __init__(
        self,
        analyze: Callable[[DiffAnalysisRequest], DiffAnalysisResponse],
        *,
        max_pending: int = 2,
        max_results: int = 8,
    ) -> None:
        self._analyze = analyze
        self._max_pending = max_pending
        self._max_results = max_results
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codegraph-diff")

    def active_count(self) -> int:
        with self._lock:
            return sum(not job.future.done() for job in self._jobs.values())

    def submit(self, request: DiffAnalysisRequest) -> dict[str, object]:
        with self._lock:
            if sum(not job.future.done() for job in self._jobs.values()) >= self._max_pending:
                raise ComparisonBusy("A comparison is already queued. Please try again shortly.")
            for key, job in list(self._jobs.items()):
                if job.future.done() and (
                    time.monotonic() - job.created_at > 3600 or len(self._jobs) >= self._max_results
                ):
                    del self._jobs[key]
            job_id = f"diff_{uuid.uuid4().hex}"
            self._jobs[job_id] = _Job(time.monotonic(), self._executor.submit(self._run, request))
        return self.get(job_id)

    def _run(self, request: DiffAnalysisRequest) -> DiffAnalysisResponse:
        try:
            return self._analyze(request)
        except Exception:
            logger.exception("Background commit comparison failed")
            raise

    def get(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
        future = job.future
        result: dict[str, object] = {"job_id": job_id}
        if not future.done():
            result["status"] = "RUNNING" if future.running() else "QUEUED"
            return result
        error = future.exception()
        if error is not None:
            result.update(
                status="FAILED",
                error=str(error)
                if isinstance(error, M21DiffError)
                else "Comparison failed. Please retry or check the server logs.",
            )
        else:
            result.update(status="COMPLETED", result=future.result().model_dump(mode="json"))
        return result

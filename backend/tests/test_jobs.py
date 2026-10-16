from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from backend.app import main
from backend.app.ingestion import AnalysisCancelled, analyze_repository, load_snapshot_analysis
from backend.app.jobs import IngestionJobManager
from backend.app.materialization import load_runtime_materialization, runtime_files_root
from backend.app.models import IngestionJob, RepositoryInfo
from backend.tests.test_ingestion import init_repo, settings


class _MemoryStore:
    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.updates: list[IngestionJob] = []
        self.published = 0
        self.runtime_materialization_updates: list[tuple[str, str]] = []

    def snapshot_status(self, snapshot_id: str) -> str | None:
        return self.status.get(snapshot_id)

    def recover_interrupted_ingestion_jobs(self) -> list[str]:
        return []

    def update_ingestion_job(self, job: IngestionJob, _repository: RepositoryInfo) -> None:
        self.updates.append(job)
        self.status[job.snapshot_id] = job.status.value

    def publish_snapshot(self, _analysis: Any, job_id: str | None = None) -> str:
        self.published += 1
        if job_id:
            job = next(item for item in reversed(self.updates) if item.job_id == job_id)
            self.status[job.snapshot_id] = "READY"
        return "test_graph"

    def garbage_collect_snapshots(self) -> list[str]:
        return []

    def update_runtime_materialization(self, snapshot_id: str, materialization_id: str) -> None:
        self.runtime_materialization_updates.append((snapshot_id, materialization_id))


def test_duplicate_active_ingestion_requests_attach_to_one_job(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)
    store = _MemoryStore()
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def processor(
        _path: str,
        _settings: object,
        _include_untracked: bool,
        *,
        cancellation_event: threading.Event,
        on_stage: Any,
        expected_snapshot_id: str,
        expected_runtime_materialization_id: str,
    ) -> object:
        nonlocal calls
        calls += 1
        on_stage("PARSING")
        started.set()
        assert release.wait(timeout=10)
        if cancellation_event.is_set():
            raise AnalysisCancelled("cancelled")
        return expected_snapshot_id

    manager = IngestionJobManager(store_factory=lambda _: store, processor=processor)
    first = manager.submit(repo, config)
    assert started.wait(timeout=10)
    duplicate = manager.submit(repo, config)

    assert duplicate.job_id == first.job_id
    assert duplicate.snapshot_id == first.snapshot_id
    assert duplicate.attached
    assert calls == 1

    release.set()
    final = manager.wait(first.snapshot_id, timeout=10)
    assert final.status.value == "COMPLETED"
    assert store.published == 1


def test_cancellation_stops_before_publish_and_never_marks_snapshot_ready(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)
    store = _MemoryStore()
    started = threading.Event()
    cancelled = threading.Event()

    def processor(
        _path: str,
        _settings: object,
        _include_untracked: bool,
        *,
        cancellation_event: threading.Event,
        on_stage: Any,
        expected_snapshot_id: str,
        expected_runtime_materialization_id: str,
    ) -> object:
        on_stage("SEMANTIC_INDEXING")
        started.set()
        while not cancelled.wait(timeout=0.01):
            if cancellation_event.is_set():
                cancelled.set()
        raise AnalysisCancelled("cancelled")

    manager = IngestionJobManager(store_factory=lambda _: store, processor=processor)
    job = manager.submit(repo, config)
    assert started.wait(timeout=10)

    requested = manager.cancel(job.snapshot_id)
    assert requested.cancel_requested
    final = manager.wait(job.snapshot_id, timeout=10)

    assert final.status.value == "CANCELLED"
    assert store.published == 0
    assert "READY" not in store.status.values()


def test_ready_static_snapshot_refreshes_runtime_materialization_without_mutating_old_tree(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "fixture.json").write_text('{"value": "old"}\n', encoding="utf-8")
    # Track the runtime-only asset without changing the source analysis inputs.
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "fixture.json"], check=True)
    config = settings(tmp_path)
    first_analysis = analyze_repository(repo, config)
    old_id = first_analysis.runtime_materialization_id
    assert old_id is not None
    old_materialization = load_runtime_materialization(
        config.snapshot_cache_dir, first_analysis.snapshot_id, old_id
    )
    store = _MemoryStore()
    store.status[first_analysis.snapshot_id] = "READY"

    (repo / "fixture.json").write_text('{"value": "new"}\n', encoding="utf-8")
    manager = IngestionJobManager(store_factory=lambda _: store)
    response = manager.submit(repo, config)

    refreshed = load_snapshot_analysis(first_analysis.snapshot_id, config)
    assert response.status.value == "COMPLETED"
    assert refreshed.snapshot_id == first_analysis.snapshot_id
    assert refreshed.runtime_materialization_id != old_id
    assert store.runtime_materialization_updates == [
        (first_analysis.snapshot_id, refreshed.runtime_materialization_id)
    ]
    assert (
        load_runtime_materialization(config.snapshot_cache_dir, first_analysis.snapshot_id, old_id)
        == old_materialization
    )
    new_materialization = load_runtime_materialization(
        config.snapshot_cache_dir,
        first_analysis.snapshot_id,
        refreshed.runtime_materialization_id,
    )
    assert next(item for item in new_materialization.files if item.path == "fixture.json")
    assert (
        runtime_files_root(
            config.snapshot_cache_dir,
            first_analysis.snapshot_id,
            refreshed.runtime_materialization_id,
        )
        / "fixture.json"
    ).read_text(encoding="utf-8") == '{"value": "new"}\n'


def test_application_startup_runs_interrupted_job_recovery(
    monkeypatch: Any,
) -> None:
    recovered: list[str] = []

    def recover(_settings: object) -> list[str]:
        recovered.append("called")
        return ["job_interrupted"]

    monkeypatch.setattr(main.ingestion_jobs, "recover_interrupted_jobs", recover)

    async def exercise_lifespan() -> None:
        async with main.app.router.lifespan_context(main.app):
            pass

    asyncio.run(exercise_lifespan())

    assert recovered == ["called"]

import threading
import time
from types import SimpleNamespace

import pytest

from backend.app.diff_analysis import DiffAnalysisRequest, M21DiffError
from backend.app.diff_jobs import ComparisonBusy, DiffJobManager

REQUEST = DiffAnalysisRequest(repository_path="/repo", base_sha="a" * 40, target_sha="b" * 40)


def wait_done(manager, job_id):
    for _ in range(200):
        result = manager.get(job_id)
        if result["status"] in {"COMPLETED", "FAILED"}:
            return result
        time.sleep(0.005)
    raise AssertionError("Worker did not finish")


def test_queued_work_is_bounded_and_results_keep_identity():
    entered = threading.Event()
    release = threading.Event()

    def analyze(request):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(model_dump=lambda **kwargs: {"target": request.target_sha})

    manager = DiffJobManager(analyze, max_pending=2)
    first = manager.submit(REQUEST)
    assert entered.wait(1)
    second = manager.submit(REQUEST)
    assert manager.get(second["job_id"])["status"] == "QUEUED"
    assert manager.active_count() == 2
    with pytest.raises(ComparisonBusy):
        manager.submit(REQUEST)
    release.set()
    completed = wait_done(manager, first["job_id"])
    assert completed["result"]["target"] == REQUEST.target_sha
    assert completed["job_id"] == first["job_id"]
    wait_done(manager, second["job_id"])
    assert manager.active_count() == 0
    with pytest.raises(KeyError):
        manager.get("diff_unknown")


def test_failures_are_reported_without_exposing_unexpected_exception_details():
    def expected(request):
        raise M21DiffError("Commit is not available")

    manager = DiffJobManager(expected)
    job = manager.submit(REQUEST)
    assert wait_done(manager, job["job_id"])["error"] == "Commit is not available"

    def unexpected(request):
        raise RuntimeError("private internal details")

    manager = DiffJobManager(unexpected)
    job = manager.submit(REQUEST)
    result = wait_done(manager, job["job_id"])
    assert result["status"] == "FAILED"
    assert "private internal details" not in result["error"]


def test_http_job_endpoints_and_unknown_job(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app import main

    manager = DiffJobManager(
        lambda request: SimpleNamespace(model_dump=lambda **kwargs: {"done": True})
    )
    monkeypatch.setattr(main, "diff_jobs", manager)
    c = TestClient(main.app)
    r = c.post("/api/diff/jobs", json=REQUEST.model_dump())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    wait_done(manager, job_id)
    assert c.get("/api/diff/jobs/" + job_id).json()["result"] == {"done": True}
    assert c.get("/api/diff/jobs/unknown").status_code == 404
    assert c.post("/api/diff/jobs", json={"repository_path": "/repo"}).status_code == 422

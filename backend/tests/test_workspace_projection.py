import json

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.tests.test_graph_tools import SNAPSHOT, _analysis


def test_workspace_retains_complete_evidence_and_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _analysis()
    monkeypatch.setattr(main, "snapshot_analysis", lambda _snapshot: analysis)
    response = main.snapshot_workspace(SNAPSHOT)
    payload = json.loads(bytes(response.body))
    full = analysis.model_dump(mode="json")
    assert payload == {field: full[field] for field in main.WORKSPACE_FIELDS}
    for field in ("entities", "sites", "bindings", "derived_edges", "provenance"):
        assert payload[field] == full[field]
    assert "retrieval_chunks" not in payload
    assert "commits" not in payload
    assert response.media_type == "application/json"


@pytest.mark.parametrize("status", [400, 404])
def test_workspace_preserves_analysis_errors(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def fail(_snapshot: str) -> None:
        raise HTTPException(status_code=status, detail="Unavailable snapshot")

    monkeypatch.setattr(main, "snapshot_analysis", fail)
    with pytest.raises(HTTPException) as error:
        main.snapshot_workspace(SNAPSHOT)
    assert error.value.status_code == status


def test_source_context_keeps_exact_span_and_verifies_blob(tmp_path, monkeypatch) -> None:
    import hashlib

    from backend.app.config import Settings
    from backend.app.models import SnapshotManifestEntry

    content = b"# context\ndef greet():\n    return normalize('hello')\n# after\n"
    digest = hashlib.sha256(content).hexdigest()
    analysis = _analysis().model_copy(
        update={
            "manifest": [
                SnapshotManifestEntry(
                    path="demo.py",
                    content_hash=digest,
                    blob_hash=digest,
                    size_bytes=len(content),
                    analyzed=True,
                )
            ]
        }
    )
    directory = tmp_path / SNAPSHOT / "blobs"
    directory.mkdir(parents=True)
    blob = directory / digest
    blob.write_bytes(content)
    monkeypatch.setattr(main, "settings", Settings(snapshot_cache_dir=tmp_path))
    monkeypatch.setattr(main, "load_snapshot_analysis", lambda *_args: analysis)
    start = content.index(b"normalize")
    exact = main.source_detail(SNAPSHOT, "demo.py", start, start + 9)
    context = main.source_detail(SNAPSHOT, "demo.py", start, start + 9, context_lines=3)
    assert {key: context[key] for key in exact} == exact
    assert context["context"]["start_line"] == 1
    assert "    return normalize('hello')" in context["context"]["lines"]
    with pytest.raises(HTTPException) as invalid:
        main.source_detail(SNAPSHOT, "demo.py", start, start + 9, context_lines=6)
    assert invalid.value.status_code == 422
    blob.write_bytes(b"tampered")
    with pytest.raises(HTTPException) as tampered:
        main.source_detail(SNAPSHOT, "demo.py", start, start + 9, context_lines=3)
    assert tampered.value.status_code == 409


def test_investigation_snapshot_io_does_not_block_event_loop(monkeypatch) -> None:
    import asyncio
    import time

    from backend.app.main import QueryExecutionRequest
    from backend.tests.test_query_api import FakeStore

    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore("READY"))

    def slow_load(*_args):
        time.sleep(0.2)
        raise FileNotFoundError("Missing analysis")

    monkeypatch.setattr(main, "load_snapshot_analysis", slow_load)

    async def exercise():
        task = asyncio.create_task(
            main.query_investigate(
                QueryExecutionRequest(snapshot_id=SNAPSHOT, question="Who calls caller?")
            )
        )
        await asyncio.sleep(0.03)
        assert not task.done(), "Snapshot I/O blocked all other event-loop tasks"
        with pytest.raises(HTTPException) as error:
            await task
        assert error.value.status_code == 404

    asyncio.run(exercise())

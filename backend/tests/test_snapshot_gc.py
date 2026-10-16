from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.app import main
from backend.app.config import Settings
from backend.app.graph import GraphStore
from backend.app.ingestion import load_snapshot_analysis
from backend.app.lifecycle import register_active_snapshot, unregister_active_snapshot


class _Result:
    def __init__(self, rows: list[list[Any]] | None = None):
        self.result_set = rows or []


class _MetadataGraph:
    def __init__(
        self, snapshots: dict[str, dict[str, Any]], pins: set[str], jobs: list[dict[str, Any]]
    ):
        self.snapshots = snapshots
        self.pins = pins
        self.jobs = jobs

    def query(self, query: str, params: dict[str, Any] | None = None) -> _Result:
        values = params or {}
        if "RETURN s.snapshot_id, s.repository_id, s.graph_name, s.status" in query:
            return _Result(
                [
                    [
                        record["snapshot_id"],
                        record["repository_id"],
                        record["graph_name"],
                        record["status"],
                        record.get("ready_at"),
                        record.get("terminal_at"),
                        record.get("updated_at"),
                        record.get("manually_pinned", False),
                    ]
                    for record in self.snapshots.values()
                ]
            )
        if "MATCH (pin)-[:PINS]" in query:
            return _Result([[snapshot_id] for snapshot_id in sorted(self.pins)])
        if "RETURN s.graph_name, s.status" in query:
            snapshot = self.snapshots.get(str(values["snapshot_id"]))
            return _Result([[snapshot["graph_name"], snapshot["status"]]] if snapshot else [])
        if "MATCH (j:IngestionJob) WHERE j.status IN" in query:
            active = {
                "QUEUED",
                "MATERIALIZING",
                "PARSING",
                "SEMANTIC_INDEXING",
                "VALIDATING",
                "PUBLISHING",
            }
            return _Result([[job["snapshot_id"]] for job in self.jobs if job["status"] in active])
        snapshot_id = str(values.get("snapshot_id", ""))
        snapshot = self.snapshots.get(snapshot_id)
        if "SET s.terminal_at = $now" in query:
            if snapshot:
                snapshot["terminal_at"] = values["now"]
                snapshot["updated_at"] = snapshot.get("updated_at") or values["now"]
            return _Result()
        if "SET s.status = 'GC_PENDING'" in query:
            if snapshot and snapshot["status"] == values["expected_status"]:
                snapshot["status"] = "GC_PENDING"
                snapshot["gc_marked_at"] = values["now"]
                return _Result([[snapshot["graph_name"]]])
            return _Result()
        if "DETACH DELETE s" in query:
            if snapshot and snapshot["status"] == "GC_PENDING":
                del self.snapshots[snapshot_id]
            return _Result()
        if "SET s.manually_pinned" in query:
            if snapshot:
                snapshot["manually_pinned"] = values["pinned"]
                return _Result([[snapshot_id]])
            return _Result()
        return _Result()


class _Graph:
    def __init__(self, name: str, graphs: _FakeClient):
        self.name = name
        self.graphs = graphs

    def delete(self) -> None:
        self.graphs.graph_names.discard(self.name)


class _FakeClient:
    def __init__(self, metadata: _MetadataGraph, graph_names: set[str]):
        self.metadata = metadata
        self.graph_names = graph_names

    def select_graph(self, name: str) -> Any:
        if name == "codegraph_metadata":
            return self.metadata
        return _Graph(name, self)

    def list_graphs(self) -> list[str]:
        return sorted(self.graph_names)


def _snapshot(
    snapshot_id: str,
    repository_id: str,
    status: str = "READY",
    *,
    ready_at: float | None = None,
    terminal_at: float | None = None,
    manually_pinned: bool = False,
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "repository_id": repository_id,
        "graph_name": f"graph_{snapshot_id}",
        "status": status,
        "ready_at": ready_at,
        "terminal_at": terminal_at,
        "updated_at": terminal_at,
        "manually_pinned": manually_pinned,
    }


def _store(
    snapshots: list[dict[str, Any]],
    *,
    pins: set[str] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    grace_seconds: int = 7 * 24 * 60 * 60,
    cache_dir: Path | None = None,
) -> tuple[GraphStore, _MetadataGraph, _FakeClient]:
    records = {snapshot["snapshot_id"]: snapshot for snapshot in snapshots}
    metadata = _MetadataGraph(records, pins or set(), jobs or [])
    names = {snapshot["graph_name"] for snapshot in snapshots if snapshot["graph_name"]}
    client = _FakeClient(metadata, names)
    store = GraphStore.__new__(GraphStore)
    store.client = client
    store.settings = Settings(
        failed_snapshot_gc_grace_seconds=grace_seconds,
        snapshot_cache_dir=cache_dir or Path("/private/tmp/codegraph-gc-test-cache"),
    )
    return store, metadata, client


def test_ready_retention_keeps_newest_five_and_isolated_by_repository() -> None:
    repo_a = [_snapshot(f"a{i}", "repo-a", ready_at=float(i)) for i in range(1, 8)]
    repo_b = [_snapshot(f"b{i}", "repo-b", ready_at=float(i)) for i in range(1, 3)]
    store, metadata, client = _store([*repo_a, *repo_b])

    deleted = store.garbage_collect_snapshots(now=100)

    assert deleted == ["a1", "a2"]
    assert set(metadata.snapshots) == {"a3", "a4", "a5", "a6", "a7", "b1", "b2"}
    assert "graph_a1" not in client.graph_names
    assert "graph_b1" in client.graph_names


def test_old_manually_or_record_pinned_snapshots_survive_retention() -> None:
    snapshots = [
        _snapshot(f"s{i}", "repo", ready_at=float(i), manually_pinned=(i == 1))
        for i in range(1, 10)
    ]
    store, metadata, _ = _store(snapshots, pins={"s2"})

    deleted = store.garbage_collect_snapshots(now=100)

    assert deleted == ["s3", "s4"]
    assert {"s1", "s2", "s5", "s6", "s7", "s8", "s9"} <= set(metadata.snapshots)


def test_failed_and_cancelled_graphs_obey_configured_grace_period() -> None:
    snapshots = [
        _snapshot("failed-old", "repo", "FAILED", terminal_at=800),
        _snapshot("cancelled-old", "repo", "CANCELLED", terminal_at=800),
        _snapshot("failed-recent", "repo", "FAILED", terminal_at=950),
    ]
    store, metadata, _ = _store(snapshots, grace_seconds=100)

    deleted = store.garbage_collect_snapshots(now=1_000)

    assert deleted == ["cancelled-old", "failed-old"]
    assert set(metadata.snapshots) == {"failed-recent"}


def test_active_snapshots_are_never_collected() -> None:
    snapshots = [
        _snapshot("validating", "repo", "VALIDATING"),
        _snapshot("active-failed", "repo", "FAILED", terminal_at=1),
        *[_snapshot(f"ready{i}", "repo", "READY", ready_at=float(i)) for i in range(1, 8)],
    ]
    jobs = [{"snapshot_id": "active-failed", "status": "PARSING"}]
    store, metadata, _ = _store(snapshots, jobs=jobs)
    register_active_snapshot("ready1")
    try:
        assert store.garbage_collect_snapshots(now=1_000_000) == ["ready2"]
    finally:
        unregister_active_snapshot("ready1")
    assert set(metadata.snapshots) == {
        "validating",
        "active-failed",
        "ready1",
        "ready3",
        "ready4",
        "ready5",
        "ready6",
        "ready7",
    }


def test_gc_is_safe_to_repeat_and_resumes_gc_pending_records() -> None:
    snapshots = [
        *[_snapshot(f"ready{i}", "repo", ready_at=float(i)) for i in range(1, 7)],
        _snapshot("pending", "other", "GC_PENDING"),
    ]
    store, metadata, client = _store(snapshots)

    first = store.garbage_collect_snapshots(now=100)
    second = store.garbage_collect_snapshots(now=100)

    assert first == ["pending", "ready1"]
    assert second == []
    assert set(metadata.snapshots) == {"ready2", "ready3", "ready4", "ready5", "ready6"}
    assert "graph_pending" not in client.graph_names


def test_gc_removes_all_snapshot_cache_material_and_prevents_cache_resurrection(
    tmp_path: Path,
) -> None:
    obsolete_id = "snap_" + "a" * 64
    snapshots = [
        _snapshot(obsolete_id, "repo", "READY", ready_at=1),
        *[
            _snapshot("snap_" + str(i) * 64, "repo", "READY", ready_at=float(i + 1))
            for i in range(1, 6)
        ],
    ]
    cache_root = tmp_path / obsolete_id
    (cache_root / "blobs").mkdir(parents=True)
    (cache_root / "runtime" / "materializations").mkdir(parents=True)
    (cache_root / "analysis.json").write_text("{}", encoding="utf-8")
    (cache_root / "manifest.json").write_text("{}", encoding="utf-8")
    (cache_root / "blobs" / "source-blob").write_text("source", encoding="utf-8")
    (cache_root / "runtime" / "materializations" / "stale").mkdir()
    store, _metadata, _client = _store(snapshots, cache_dir=tmp_path)

    assert store.garbage_collect_snapshots(now=10) == [obsolete_id]
    assert not cache_root.exists()
    assert store.garbage_collect_snapshots(now=10) == []
    with pytest.raises(FileNotFoundError, match="Snapshot analysis"):
        load_snapshot_analysis(obsolete_id, Settings(snapshot_cache_dir=tmp_path))


def test_manual_pin_api_is_idempotent_and_unpins_when_requested() -> None:
    store, metadata, _ = _store([_snapshot("snap", "repo")])

    store.set_snapshot_pinned("snap")
    store.set_snapshot_pinned("snap")
    assert metadata.snapshots["snap"]["manually_pinned"] is True
    store.set_snapshot_pinned("snap", False)
    assert metadata.snapshots["snap"]["manually_pinned"] is False


def test_manual_pin_route_updates_retention_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    class Store:
        def __init__(self, _settings: Settings) -> None:
            pass

        def set_snapshot_pinned(self, snapshot_id: str, pinned: bool) -> None:
            calls.append((snapshot_id, pinned))

    monkeypatch.setattr(main, "GraphStore", Store)
    result = main.snapshot_pin("snap-1", main.SnapshotPinRequest(pinned=True))

    assert result == {"snapshot_id": "snap-1", "pinned": True}
    assert calls == [("snap-1", True)]


@pytest.mark.parametrize(
    "status",
    [
        "DISCOVERED",
        "MATERIALIZING",
        "PARSING",
        "SEMANTIC_INDEXING",
        "VALIDATING",
        "FAILED",
        "CANCELLED",
        "GC_PENDING",
    ],
)
def test_unpublished_or_gc_pending_snapshots_cannot_be_queried(status: str) -> None:
    store, _, _ = _store([_snapshot("hidden", "repo", status)])

    with pytest.raises(KeyError, match="Only READY"):
        store.snapshot_graph("hidden")

import os
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.ingestion import RepositoryError, _cached_analysis, load_snapshot_analysis
from backend.app.models import ReplayOutcome
from backend.tests.test_graph_tools import _analysis

SNAPSHOT = "snap_" + "a" * 64


def write_analysis(root: Path, *, snapshot=SNAPSHOT):
    path = root / SNAPSHOT / "analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    analysis = _analysis().model_copy(update={"snapshot_id": snapshot})
    path.write_text(analysis.model_dump_json(), encoding="utf-8")
    return path


def test_cached_analysis_never_shares_mutable_records(tmp_path):
    _cached_analysis.cache_clear()
    write_analysis(tmp_path)
    settings = Settings(snapshot_cache_dir=tmp_path)
    first = load_snapshot_analysis(SNAPSHOT, settings)
    expected = first.entities[0].name
    first.entities[0].name = "changed by caller"
    first.counts["unexpected"] = 42
    second = load_snapshot_analysis(SNAPSHOT, settings)
    assert second.entities[0].name == expected
    assert "unexpected" not in second.counts
    assert _cached_analysis.cache_info().hits == 1


def test_atomic_replay_update_invalidates_even_with_same_mtime_and_size(tmp_path):
    _cached_analysis.cache_clear()
    path = write_analysis(tmp_path)
    settings = Settings(snapshot_cache_dir=tmp_path)
    original = load_snapshot_analysis(SNAPSHOT, settings)
    before = path.stat()
    updated = original.model_copy(deep=True)
    updated.provenance[0].replay_state = ReplayOutcome.PASS
    temporary = path.with_suffix(".tmp")
    temporary.write_text(updated.model_dump_json().ljust(before.st_size), encoding="utf-8")
    assert temporary.stat().st_size == before.st_size
    os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
    temporary.replace(path)
    assert load_snapshot_analysis(SNAPSHOT, settings).provenance[0].replay_state == "PASS"


def test_cache_isolated_by_root_and_checks_snapshot_identity(tmp_path):
    _cached_analysis.cache_clear()
    write_analysis(tmp_path / "a")
    other = write_analysis(tmp_path / "b", snapshot="snap_" + "b" * 64)
    load_snapshot_analysis(SNAPSHOT, Settings(snapshot_cache_dir=tmp_path / "a"))
    with pytest.raises(RepositoryError, match="identity mismatch"):
        load_snapshot_analysis(SNAPSHOT, Settings(snapshot_cache_dir=tmp_path / "b"))
    other.unlink()
    with pytest.raises(FileNotFoundError):
        load_snapshot_analysis(SNAPSHOT, Settings(snapshot_cache_dir=tmp_path / "b"))

from pathlib import Path
from types import SimpleNamespace

from backend.app.config import Settings
from backend.app.replay_availability import replay_availability
from backend.tests.test_graph_tools import _analysis


def test_missing_record_and_missing_source_are_unavailable(tmp_path: Path) -> None:
    analysis = _analysis()
    settings = Settings(snapshot_cache_dir=tmp_path)
    assert replay_availability(analysis, "missing", settings)["available"] is False
    assert (
        replay_availability(analysis, analysis.provenance[0].edge_id, settings)["available"]
        is False
    )


def test_prerequisites_never_run_analysis_and_do_not_claim_verification(tmp_path, monkeypatch):
    from backend.app import replay_availability as module

    analysis = _analysis()
    record = analysis.provenance[0].model_copy(update={"extractor_name": "scip-python"})
    manifest = SimpleNamespace(analyzed=True, blob_hash="a" * 64)
    analysis = SimpleNamespace(
        provenance=[record],
        manifest=[manifest],
        snapshot_id="snapshot",
        analysis_environment=SimpleNamespace(
            source_manifest_hash="manifest", environment_id="original"
        ),
    )
    root = tmp_path / "snapshot" / "blobs"
    root.mkdir(parents=True)
    (root / manifest.blob_hash).write_text("saved source")
    settings = Settings(snapshot_cache_dir=tmp_path)
    monkeypatch.setattr(module, "scip_toolchain", lambda _: (None, None, "missing", "missing"))
    assert (
        "not configured" in module.replay_availability(analysis, record.edge_id, settings)["reason"]
    )
    monkeypatch.setattr(module, "scip_toolchain", lambda _: ("python", "cli", "1", "1"))
    monkeypatch.setattr(
        module, "capture_environment", lambda *_: SimpleNamespace(environment_id="changed")
    )
    assert module.replay_availability(analysis, record.edge_id, settings)["available"] is False
    monkeypatch.setattr(
        module, "capture_environment", lambda *_: SimpleNamespace(environment_id="original")
    )
    result = module.replay_availability(analysis, record.edge_id, settings)
    assert result["available"] is True
    assert "inconclusive" in result["reason"]
    assert record.replay_state == "NOT_RUN"

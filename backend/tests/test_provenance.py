from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import main, provenance
from backend.app.config import Settings
from backend.app.ingestion import analyze_repository, load_snapshot_analysis
from backend.app.models import (
    AnalysisEnvironment,
    ReplayOutcome,
    ReplayResult,
    ReplayScope,
    ReviewState,
)
from backend.app.provenance import replay_edge
from backend.tests.test_ingestion import init_repo


def test_file_span_replay_passes_then_detects_tampered_cached_source(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    record = next(item for item in analysis.provenance if item.rule_id == "syntax.site-context.v1")

    result = replay_edge(analysis, record.edge_id, settings)

    assert result.outcome == ReplayOutcome.PASS
    assert result.scope == "FILE_OR_SPAN"
    blob = (
        settings.snapshot_cache_dir
        / analysis.snapshot_id
        / "blobs"
        / next(item.blob_hash for item in analysis.manifest if item.path == record.file_path)
    )
    blob.write_bytes(blob.read_bytes() + b"# tampered\n")
    tampered = replay_edge(analysis, record.edge_id, settings)
    assert tampered.outcome == ReplayOutcome.SOURCE_HASH_MISMATCH
    assert tampered.review_state == ReviewState.REPLAY_FAILED


def test_same_extraction_reproduces_identical_structural_edge_ids(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )

    first = analyze_repository(repo, settings)
    second = analyze_repository(repo, settings)

    assert [item.edge_id for item in first.provenance] == [
        item.edge_id for item in second.provenance
    ]


def test_unknown_replay_identity_is_inconclusive_error_not_edge_mismatch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)

    result = replay_edge(analysis, "not-an-evidence-id", settings)

    assert result.outcome == ReplayOutcome.ERROR
    assert "not found" in result.message


def test_external_import_replay_reports_environment_mismatch_as_inconclusive(
    tmp_path: Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="scip-python",
        scip_cli_bin="scip",
    )
    analysis = analyze_repository(repo, settings)
    import_record = next(
        item for item in analysis.provenance if item.rule_id.startswith("imports.")
    )
    environment = AnalysisEnvironment(
        environment_id="recorded-environment",
        python_version="3.11",
        platform="test",
        python_executable_hash="python",
        sys_path_hash="sys-path",
        installed_distributions_hash="distributions",
        scip_python_version="recorded-scip-python",
        scip_cli_version="recorded-scip",
        node_version="node",
        source_manifest_hash="manifest",
    )
    semantic_import = import_record.model_copy(
        update={"extractor_name": "scip-python", "extractor_version": "recorded-scip-python"}
    )
    analysis = analysis.model_copy(
        update={
            "analysis_environment": environment,
            "provenance": [
                semantic_import if item.edge_id == import_record.edge_id else item
                for item in analysis.provenance
            ],
        }
    )
    monkeypatch.setattr(
        provenance,
        "scip_toolchain",
        lambda _settings: ("scip-python", "scip", "3.11", "current-scip"),
    )
    monkeypatch.setattr(
        provenance,
        "capture_environment",
        lambda *_args: environment.model_copy(update={"environment_id": "different-environment"}),
    )
    monkeypatch.setattr(
        provenance,
        "run_scip_index",
        lambda *_args: pytest.fail("environment mismatch must stop before SCIP replay"),
    )

    result = replay_edge(analysis, semantic_import.edge_id, settings)

    assert result.outcome == ReplayOutcome.ENVIRONMENT_MISMATCH
    assert result.verified_environment_id == "different-environment"


def test_replay_endpoint_persists_replay_and_review_state(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    record = next(item for item in analysis.provenance if item.rule_id == "syntax.site-context.v1")

    class NoPublishedGraph:
        def update_provenance_replay(self, *_args) -> None:
            raise KeyError("Snapshot is not registered in FalkorDB")

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "GraphStore", lambda _settings: NoPublishedGraph())

    result = main.evidence_replay(
        record.edge_id,
        main.EvidenceReplayRequest(snapshot_id=analysis.snapshot_id),
    )
    assert result["outcome"] == ReplayOutcome.PASS.value
    saved = load_snapshot_analysis(analysis.snapshot_id, settings)
    saved_record = next(item for item in saved.provenance if item.edge_id == record.edge_id)
    assert saved_record.replay_state == ReplayOutcome.PASS
    assert saved_record.review_state == ReviewState.NONE

    entry = next(item for item in analysis.manifest if item.path == record.file_path)
    blob = settings.snapshot_cache_dir / analysis.snapshot_id / "blobs" / entry.blob_hash
    original_content = blob.read_bytes()
    blob.write_bytes(original_content + b"# tampered\n")
    failed = main.evidence_replay(
        record.edge_id,
        main.EvidenceReplayRequest(snapshot_id=analysis.snapshot_id),
    )
    failed_record = next(
        item
        for item in load_snapshot_analysis(analysis.snapshot_id, settings).provenance
        if item.edge_id == record.edge_id
    )
    assert failed["outcome"] == ReplayOutcome.SOURCE_HASH_MISMATCH.value
    assert failed_record.replay_state == ReplayOutcome.SOURCE_HASH_MISMATCH
    assert failed_record.review_state == ReviewState.REPLAY_FAILED

    monkeypatch.setattr(
        main,
        "replay_edge",
        lambda *_args: ReplayResult(
            edge_id=record.edge_id,
            scope=ReplayScope.FILE_OR_SPAN,
            outcome=ReplayOutcome.ERROR,
            review_state=ReviewState.REPLAY_FAILED,
            message="temporary replay tooling failure",
        ),
    )
    main.evidence_replay(
        record.edge_id,
        main.EvidenceReplayRequest(snapshot_id=analysis.snapshot_id),
    )
    after_transient = load_snapshot_analysis(analysis.snapshot_id, settings)
    still_failed = next(
        item for item in after_transient.provenance if item.edge_id == record.edge_id
    )
    assert still_failed.replay_state == ReplayOutcome.SOURCE_HASH_MISMATCH

    blob.write_bytes(original_content)
    monkeypatch.setattr(main, "replay_edge", replay_edge)
    recovered = main.evidence_replay(
        record.edge_id,
        main.EvidenceReplayRequest(snapshot_id=analysis.snapshot_id),
    )
    recovered_analysis = load_snapshot_analysis(analysis.snapshot_id, settings)
    recovered_record = next(
        item for item in recovered_analysis.provenance if item.edge_id == record.edge_id
    )
    assert recovered["outcome"] == ReplayOutcome.PASS.value
    assert recovered_record.replay_state == ReplayOutcome.PASS
    assert recovered_record.review_state == ReviewState.NONE


def test_replay_by_provenance_id_updates_that_provenance_record_only(
    tmp_path: Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    original = next(
        item for item in analysis.provenance if item.rule_id == "syntax.site-context.v1"
    )
    duplicate = original.model_copy(
        update={
            "provenance_id": "prov-alternate-same-edge",
            "replay_state": ReplayOutcome.ERROR,
            "review_state": ReviewState.REPLAY_FAILED,
        }
    )
    analysis = analysis.model_copy(update={"provenance": [*analysis.provenance, duplicate]})
    analysis_path = settings.snapshot_cache_dir / analysis.snapshot_id / "analysis.json"
    analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")

    class NoPublishedGraph:
        def update_provenance_replay(self, *_args) -> None:
            raise KeyError("Snapshot is not registered in FalkorDB")

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "GraphStore", lambda _settings: NoPublishedGraph())
    monkeypatch.setattr(
        main,
        "replay_edge",
        lambda _analysis, _identity, _settings: ReplayResult(
            edge_id=original.edge_id,
            scope=ReplayScope.FILE_OR_SPAN,
            outcome=ReplayOutcome.PASS,
            review_state=ReviewState.NONE,
            message="Evidence replay passed.",
        ),
    )

    main.evidence_replay(
        duplicate.provenance_id,
        main.EvidenceReplayRequest(snapshot_id=analysis.snapshot_id),
    )
    saved = load_snapshot_analysis(analysis.snapshot_id, settings)
    records = {item.provenance_id: item for item in saved.provenance}

    assert records[original.provenance_id].replay_state == ReplayOutcome.NOT_RUN
    assert records[duplicate.provenance_id].replay_state == ReplayOutcome.PASS

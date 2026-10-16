from __future__ import annotations

from typing import Any

import pytest

from backend.app.evaluation import evaluate_analysis
from backend.app.models import (
    DispatchState,
    ProvenanceRecord,
    RepositoryInfo,
    SemanticBinding,
    SiteDispatch,
    SiteKind,
    SnapshotAnalysis,
    SnapshotManifestEntry,
    SnapshotStatus,
    SourceEntity,
    SourceSite,
    SourceSpan,
)

SPAN = SourceSpan(
    start_byte=0,
    end_byte=3,
    start_line=1,
    start_column=0,
    end_line=1,
    end_column=3,
)


def _analysis() -> SnapshotAnalysis:
    entity = SourceEntity(
        entity_id="entity_target",
        kind="Function",
        name="target",
        qualified_name="pkg.target",
        file_path="pkg.py",
        span=SPAN,
    )
    import_site = SourceSite(
        site_id="site_import",
        kind=SiteKind.IMPORT_REFERENCE,
        expression="import pkg",
        file_path="pkg.py",
        span=SPAN,
        context_id="module",
        syntax_node_type="import_statement",
    )
    call_site = SourceSite(
        site_id="site_call",
        kind=SiteKind.CALL_CALLEE,
        expression="target",
        file_path="pkg.py",
        span=SPAN,
        context_id="module",
        syntax_node_type="identifier",
    )
    binding = SemanticBinding(
        binding_id="binding_call",
        edge_id="edge_call",
        site_id=call_site.site_id,
        target_entity_id=entity.entity_id,
        scip_symbol="symbol_target",
        occurrence_start_byte=0,
    )
    provenance = ProvenanceRecord(
        provenance_id="provenance_call",
        edge_id=binding.edge_id,
        snapshot_id="snap_" + "a" * 64,
        file_path="pkg.py",
        file_content_hash="hash",
        span=SPAN,
        syntax_node_type="identifier",
        scip_symbol=binding.scip_symbol,
        extractor_name="scip-python",
        extractor_version="test",
        rule_id="scip.binding.v1",
    )
    return SnapshotAnalysis(
        repository=RepositoryInfo(
            repository_id="repo", root="/repo", head_commit="commit", is_dirty=False
        ),
        snapshot_id="snap_" + "a" * 64,
        status=SnapshotStatus.VALIDATING,
        manifest=[
            SnapshotManifestEntry(
                path="pkg.py",
                content_hash="hash",
                blob_hash="blob",
                size_bytes=3,
                analyzed=True,
            )
        ],
        entities=[entity],
        sites=[import_site, call_site],
        provenance=[provenance],
        bindings=[binding],
        dispatch=[
            SiteDispatch(
                site_id=call_site.site_id,
                state=DispatchState.SINGLE_CANDIDATE,
                candidate_entity_ids=[entity.entity_id],
            )
        ],
        diagnostics=[],
        counts={},
    )


def _gold() -> dict[str, Any]:
    return {
        "annotation_version": "test-v1",
        "repository": {"name": "test", "commit_sha": "commit"},
        "definitions": ["pkg.target"],
        "imports": [{"file_path": "pkg.py", "expression": "import pkg"}],
        "exact_bindings": [
            {
                "file_path": "pkg.py",
                "start_byte": 0,
                "expression": "target",
                "kind": "CALL_CALLEE",
                "target": "pkg.target",
            }
        ],
        "dispatch_cases": [
            {
                "file_path": "pkg.py",
                "start_byte": 0,
                "expression": "target",
                "candidates": ["pkg.target"],
            }
        ],
        "unicode_spans": [
            {"file_path": "pkg.py", "start_byte": 0, "end_byte": 3, "utf8_hex": "616263"}
        ],
    }


def test_clean_annotation_has_no_errors() -> None:
    report = evaluate_analysis(_analysis(), _gold(), {"pkg.py": b"abc"})
    assert report["error_taxonomy"] == {}
    assert report["metrics"]["definitions"]["precision"] == 1.0
    assert report["metrics"]["imports"]["recall"] == 1.0


def test_multi_hop_path_requires_call_edge_with_valid_provenance_and_offset() -> None:
    analysis = _analysis()
    call = next(site for site in analysis.sites if site.kind == SiteKind.CALL_CALLEE)
    analysis = analysis.model_copy(
        update={
            "sites": [
                site.model_copy(update={"context_id": "entity_target"}) if site == call else site
                for site in analysis.sites
            ]
        }
    )
    gold = _gold() | {
        "multi_hop_questions": [
            {"id": "self-loop-path", "path": ["pkg.target", "pkg.target", "pkg.target"]}
        ],
        "dispatch_cases": [],
    }

    valid = evaluate_analysis(analysis, gold, {"pkg.py": b"abc"})
    assert valid["metrics"]["evidence_paths"]["valid"] == 1
    assert valid["metrics"]["dispatch_cases"]["measurement_status"] == "UNMEASURED"

    bad_offset = analysis.model_copy(
        update={"bindings": [analysis.bindings[0].model_copy(update={"occurrence_start_byte": 3})]}
    )
    wrong_offset_report = evaluate_analysis(bad_offset, gold, {"pkg.py": b"abc"})
    assert wrong_offset_report["metrics"]["evidence_paths"]["valid"] == 0

    non_call = analysis.model_copy(
        update={
            "sites": [
                site.model_copy(update={"kind": SiteKind.VALUE_REFERENCE})
                if site.site_id == call.site_id
                else site
                for site in analysis.sites
            ]
        }
    )
    non_call_report = evaluate_analysis(non_call, gold, {"pkg.py": b"abc"})
    assert non_call_report["metrics"]["evidence_paths"]["valid"] == 0

    duplicate_name = analysis.entities[0].model_copy(update={"entity_id": "entity_duplicate"})
    ambiguous_name = analysis.model_copy(update={"entities": [*analysis.entities, duplicate_name]})
    ambiguous_report = evaluate_analysis(ambiguous_name, gold, {"pkg.py": b"abc"})
    assert ambiguous_report["metrics"]["evidence_paths"]["valid"] == 0


@pytest.mark.parametrize(
    ("mutation", "category"),
    [
        ("false_definition", "FALSE_DEFINITION"),
        ("missing_import", "MISSING_IMPORT"),
        ("false_exact_binding", "FALSE_EXACT_BINDING"),
        ("missing_dispatch_candidate", "DISPATCH_CANDIDATE_MISMATCH"),
        ("duplicate_edge_id", "DUPLICATE_EDGE_ID"),
        ("missing_provenance", "MISSING_PROVENANCE"),
        ("bad_unicode_mapping", "UNICODE_COORDINATE_MISMATCH"),
    ],
)
def test_evaluator_detects_graph_mutations(mutation: str, category: str) -> None:
    analysis = _analysis()
    gold = _gold()
    sources = {"pkg.py": b"abc"}
    if mutation == "false_definition":
        analysis.entities.append(
            SourceEntity(
                entity_id="false",
                kind="Function",
                name="false",
                qualified_name="pkg.false",
                file_path="pkg.py",
                span=SPAN,
            )
        )
    elif mutation == "missing_import":
        analysis.sites = [site for site in analysis.sites if site.kind != SiteKind.IMPORT_REFERENCE]
    elif mutation == "false_exact_binding":
        analysis.bindings[0] = analysis.bindings[0].model_copy(
            update={"target_entity_id": "missing"}
        )
    elif mutation == "missing_dispatch_candidate":
        analysis.dispatch[0] = analysis.dispatch[0].model_copy(update={"candidate_entity_ids": []})
    elif mutation == "duplicate_edge_id":
        analysis.bindings.append(
            analysis.bindings[0].model_copy(update={"binding_id": "duplicate"})
        )
    elif mutation == "missing_provenance":
        analysis.provenance = []
    elif mutation == "bad_unicode_mapping":
        gold["unicode_spans"][0]["utf8_hex"] = "78797a"

    report = evaluate_analysis(analysis, gold, sources)
    assert category in report["error_taxonomy"]

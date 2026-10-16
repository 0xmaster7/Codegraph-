#!/usr/bin/env python3
"""Rescore retained local-model answer outputs with edge-specific graph citations.

This tool makes no model or network calls. It reconstructs each exact annotated path
from the immutable local snapshot analysis and applies the current deterministic scorer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from backend.app.config import Settings
from backend.app.graph_tools import DeterministicGraphTools, PathToolRequest
from backend.app.ingestion import load_snapshot_analysis
from backend.app.models import SnapshotStatus
from scripts.evaluate_answers_local import evaluate_claims


def build_edge_evidence(
    tools: DeterministicGraphTools,
    source_id: str,
    target_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    result = tools.find_paths(
        PathToolRequest(
            snapshot_id=snapshot_id,
            source_id=source_id,
            target_id=target_id,
            mode="STRICT_STATIC_BINDING",
            hop_limit=1,
        )
    )
    paths = [
        path
        for path in result.results
        if path.get("node_ids") == [source_id, target_id]
        and len(path.get("steps", [])) == 1
        and path["steps"][0].get("verified_graph_path") is True
    ]
    if not paths:
        raise RuntimeError(
            f"Cached snapshot has no verified strict path for {source_id} -> {target_id}"
        )
    step = paths[0]["steps"][0]
    evidence = step.get("evidence", [])
    bindings = [
        item
        for item in evidence
        if item.get("type") == "STATIC_BINDING" and item.get("snapshot_id") == snapshot_id
    ]
    if not bindings:
        raise RuntimeError(
            f"Path step has no snapshot-bound exact binding for {source_id} -> {target_id}"
        )
    return {
        "snapshot_id": snapshot_id,
        "source_id": step["source_id"],
        "target_id": step["target_id"],
        "edge_ids": step.get("edge_ids", []),
        "evidence_ids": [item["evidence_id"] for item in bindings],
        "static_binding_evidence_ids": [item["evidence_id"] for item in bindings],
        "all_evidence_ids": [item["evidence_id"] for item in evidence],
    }


def rescore_report(
    source: dict[str, Any], *, snapshot_cache: Path, source_report_sha256: str
) -> dict[str, Any]:
    snapshot_id = source["snapshot_id"]
    settings = Settings(snapshot_cache_dir=snapshot_cache)
    analysis = load_snapshot_analysis(snapshot_id, settings).model_copy(
        update={"status": SnapshotStatus.READY}
    )
    tools = DeterministicGraphTools(analysis)
    positive_records = [
        record for record in source["records"] if record["mode"].endswith("GOLD_EVIDENCE")
    ]
    for record in positive_records:
        nodes = record["graph_path_ids"]
        if len(nodes) < 2 or len(nodes) != len(record["expected_path"]):
            raise RuntimeError(f"Retained graph path is incomplete for {record['case_id']}")
        pairs = list(zip(nodes[:-1], nodes[1:], strict=True))
        edge_evidence = {pair: build_edge_evidence(tools, *pair, snapshot_id) for pair in pairs}
        record["graph_evidence_by_edge"] = [
            {
                "source_id": source_id,
                "target_id": target_id,
                **edge_evidence[(source_id, target_id)],
            }
            for source_id, target_id in pairs
        ]
        record["scores"] = evaluate_claims(record["claims"], pairs, edge_evidence, snapshot_id)

    unresolved_records = [
        record for record in source["records"] if record["mode"].endswith("NO_TARGET_EVIDENCE")
    ]
    claims = [record["scores"] for record in positive_records]
    claim_count = sum(item["claim_count"] for item in claims)
    cited_count = sum(item["validly_cited_claim_count"] for item in claims)
    result = {
        **source,
        "run_type": "deterministic rescoring of retained live-model outputs; no model calls",
        "scoring_version": "claim-specific-edge-snapshot-v2",
        "source_report_sha256": source_report_sha256,
        "source_report_scoring": source.get("run_type"),
        "rescore_method": (
            "Strict static paths were reconstructed from the local immutable SnapshotAnalysis; "
            "each claim citation is checked only against the exact expected edge's binding "
            "evidence."
        ),
        "positive_evidence_completeness": (
            sum(bool(item["evidence_complete"]) for item in claims) / len(claims)
            if claims
            else None
        ),
        "positive_citation_fidelity": cited_count / claim_count if claim_count else 0.0,
        "records": [*positive_records, *unresolved_records],
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot-cache", type=Path, required=True)
    args = parser.parse_args()
    input_bytes = args.input.read_bytes()
    source = json.loads(input_bytes)
    result = rescore_report(
        source,
        snapshot_cache=args.snapshot_cache,
        source_report_sha256=hashlib.sha256(input_bytes).hexdigest(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "source_report_sha256": result["source_report_sha256"],
                "positive_cases": result["positive_cases"],
                "positive_citation_fidelity": result["positive_citation_fidelity"],
                "positive_evidence_completeness": result["positive_evidence_completeness"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

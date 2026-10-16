#!/usr/bin/env python3
"""Evaluate local natural-language answer generation against the frozen M25 annotations.

This is a gold-evidence generation arm: the graph API resolves annotated endpoint
symbols and supplies a validated, snapshot-bound path before the local model runs.
It measures answer realization and citation behavior, not retrieval recall or the
application's unconfigured production answer-generator path.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def request_json(
    url: str, payload: dict[str, Any], timeout: float = 120
) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    return result, (time.perf_counter() - started) * 1000


def query_evidence(api: str, snapshot_id: str, path: list[str]) -> dict[str, Any]:
    def query_pair(source: str, target: str) -> tuple[dict[str, Any], dict[str, Any]]:
        result, _ = request_json(
            f"{api}/api/query",
            {
                "snapshot_id": snapshot_id,
                "question": f"Show the shortest evidence path from {source} to {target}.",
                "result_limit": 10,
            },
        )
        path_result = next(
            (
                execution["result"]["results"][0]
                for execution in result["tool_executions"]
                if execution.get("operation") == "PATHS"
                and execution.get("result", {}).get("results")
            ),
            None,
        )
        verified = any(
            occurrence.get("originating_tool") == "find_paths"
            and occurrence.get("verified_graph_path") is True
            for item in result.get("evidence_pool", {}).get("items", [])
            for occurrence in item.get("occurrences", [])
        )
        if path_result is None or not verified or result.get("snapshot_id") != snapshot_id:
            raise RuntimeError(f"No verified snapshot-bound path for {source} -> {target}")
        return result, path_result

    edge_results = [
        query_pair(source, target) for source, target in zip(path[:-1], path[1:], strict=True)
    ]
    edge_paths = [edge_path for _edge_result, edge_path in edge_results]
    return compose_annotated_path(edge_paths)


def compose_annotated_path(edge_paths: list[dict[str, Any]]) -> dict[str, Any]:
    """Compose only individually verified, contiguous edges from the frozen path."""
    if not edge_paths or any(len(edge_path.get("node_ids", [])) != 2 for edge_path in edge_paths):
        raise RuntimeError("Could not resolve every annotated adjacent edge")
    for left, right in zip(edge_paths[:-1], edge_paths[1:], strict=True):
        if left["target_id"] != right["source_id"]:
            raise RuntimeError("Annotated adjacent graph edges do not form one contiguous path")
    node_ids = [edge_paths[0]["source_id"]] + [edge_path["target_id"] for edge_path in edge_paths]
    return {
        "source_id": node_ids[0],
        "target_id": node_ids[-1],
        "node_ids": node_ids,
        "hop_count": len(edge_paths),
        "uncertainty_count": sum(edge.get("uncertainty_count", 0) for edge in edge_paths),
        "mode": "STRICT_STATIC_BINDING",
        "steps": [edge_path["steps"][0] for edge_path in edge_paths],
    }


def model_generate(ollama: str, model: str, prompt: str) -> tuple[dict[str, Any], float]:
    result, wall_ms = request_json(
        f"{ollama}/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": generation_schema(),
            "options": {"temperature": 0, "seed": 17, "num_predict": 220},
        },
    )
    try:
        generated = json.loads(result["response"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("Local model did not return schema-constrained JSON") from exc
    generated["_usage"] = {
        "prompt_eval_count": result.get("prompt_eval_count"),
        "eval_count": result.get("eval_count"),
        "total_duration_ns": result.get("total_duration"),
        "wall_latency_ms": round(wall_ms, 3),
    }
    return generated, wall_ms


def generation_schema() -> dict[str, Any]:
    """Require the same evidence-reference field that the prompt and scorer consume."""
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["source", "target", "evidence_refs"],
                },
            },
            "abstain": {"type": "boolean"},
            "uncertainty": {"type": "string"},
        },
        "required": ["answer", "claims", "abstain", "uncertainty"],
    }


def positive_prompt(
    question: str, path: list[str], evidence: dict[str, Any]
) -> tuple[str, dict[str, str], dict[str, str]]:
    node_aliases = {f"n{index}": node_id for index, node_id in enumerate(evidence["node_ids"])}
    evidence_aliases: dict[str, str] = {}
    edges = []
    for index, step in enumerate(evidence["steps"]):
        binding = next(item for item in step["evidence"] if item["type"] == "STATIC_BINDING")
        ref = f"e{index}"
        evidence_aliases[ref] = binding["evidence_id"]
        edges.append(
            {
                "source": f"n{index}",
                "target": f"n{index + 1}",
                "relation": step["relation"],
                "evidence_ref": ref,
            }
        )
    symbol_names = {f"n{index}": name for index, name in enumerate(path)}
    prompt = (
        "Answer the user's question only from the verified graph evidence below. "
        "Do not infer runtime behavior; these are static call-site bindings. "
        "Return concise JSON with answer, claims (one object per path edge using the exact "
        "node aliases and evidence reference shown), abstain=false, and an empty "
        "uncertainty string.\nQUESTION: "
        + question
        + "\nANNOTATED PATH SYMBOLS: "
        + json.dumps(symbol_names, ensure_ascii=False, separators=(",", ":"))
        + "\nVERIFIED PATH EDGES: "
        + json.dumps(edges, separators=(",", ":"))
    )
    return prompt, node_aliases, evidence_aliases


def unknown_prompt(case: dict[str, Any]) -> str:
    item = {
        "expression": case["expression"],
        "context": case["context"],
        "file_path": case["file_path"],
        "line": case["line"],
        "classification": case["classification"],
        "resolved_target_evidence": [],
    }
    return (
        "Answer only from the evidence below. The call target is explicitly unresolved and no "
        "target evidence is supplied. Do not guess from names or Python conventions. Return JSON "
        "with a concise uncertainty-aware answer, claims=[], abstain=true, and uncertainty that "
        "the target cannot be established from this evidence.\nCASE: "
        + json.dumps(item, ensure_ascii=False)
    )


def evaluate_claims(
    claims: list[dict[str, Any]],
    expected: list[tuple[str, str]],
    evidence_by_edge: dict[tuple[str, str], dict[str, Any]],
    snapshot_id: str,
) -> dict[str, Any]:
    correct_pairs = [(claim.get("source"), claim.get("target")) for claim in claims]
    supported = sum(pair in expected for pair in correct_pairs)
    valid_citations = []
    for claim, pair in zip(claims, correct_pairs, strict=True):
        cited_ids = claim.get("evidence_ids", [])
        pair_key: tuple[str, str] | None = None
        if (
            isinstance(pair, tuple)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
        ):
            pair_key = (pair[0], pair[1])
        expected_edge = evidence_by_edge.get(pair_key) if pair_key is not None else None
        valid_citations.append(
            isinstance(cited_ids, list)
            and bool(cited_ids)
            and all(isinstance(evidence_id, str) for evidence_id in cited_ids)
            and len(cited_ids) == len(set(cited_ids))
            and expected_edge is not None
            and expected_edge.get("snapshot_id") == snapshot_id
            and set(cited_ids).issubset(set(expected_edge.get("evidence_ids", [])))
        )
    cited = sum(valid_citations)
    exact_path = correct_pairs == expected
    return {
        "claim_count": len(claims),
        "supported_claim_count": supported,
        "unsupported_claim_count": len(claims) - supported,
        "unsupported_claim_rate": (len(claims) - supported) / len(claims) if claims else 0.0,
        "validly_cited_claim_count": cited,
        "citation_fidelity": cited / len(claims) if claims else 0.0,
        "evidence_complete": (
            exact_path and len(valid_citations) == len(expected) and all(valid_citations)
        ),
        "expected_path_exact": exact_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("docs/evaluation/requests-v2.32.5.annotations.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/answers-local-qwen25-05b.json")
    )
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen2.5:0.5b")
    parser.add_argument("--limit-positive", type=int)
    parser.add_argument("--limit-unknown", type=int)
    args = parser.parse_args()

    annotations = json.loads(args.annotations.read_text())
    positives = annotations["multi_hop_questions"]
    unknowns = annotations["unresolved_dynamic_cases"]
    if args.limit_positive is not None:
        positives = positives[: args.limit_positive]
    if args.limit_unknown is not None:
        unknowns = unknowns[: args.limit_unknown]

    records: list[dict[str, Any]] = []
    for question in positives:
        expected_path = question["path"]
        evidence = query_evidence(args.api, args.snapshot, expected_path)
        expected_pairs = list(zip(evidence["node_ids"][:-1], evidence["node_ids"][1:], strict=True))
        evidence_by_edge: dict[tuple[str, str], dict[str, Any]] = {}
        for pair, step in zip(expected_pairs, evidence["steps"], strict=True):
            bindings = [
                item
                for item in step["evidence"]
                if item.get("type") == "STATIC_BINDING" and item.get("snapshot_id") == args.snapshot
            ]
            evidence_by_edge[pair] = {
                "snapshot_id": args.snapshot,
                "evidence_ids": [item["evidence_id"] for item in bindings],
                "edge_ids": step.get("edge_ids", []),
                "source_id": step["source_id"],
                "target_id": step["target_id"],
            }
        valid_evidence = {
            evidence_id
            for edge in evidence_by_edge.values()
            for evidence_id in edge["evidence_ids"]
        }
        prompt, node_aliases, evidence_aliases = positive_prompt(
            question["question"], expected_path, evidence
        )
        generated, _ = model_generate(args.ollama, args.model, prompt)
        raw_claims = generated["claims"]
        normalized_claims = [
            {
                "source": node_aliases.get(claim.get("source"), claim.get("source")),
                "target": node_aliases.get(claim.get("target"), claim.get("target")),
                "evidence_ids": [
                    evidence_aliases.get(ref, ref) for ref in claim.get("evidence_refs", [])
                ],
            }
            for claim in raw_claims
        ]
        records.append(
            {
                "case_id": question["id"],
                "mode": "LOCAL_LIVE_MODEL_GOLD_EVIDENCE",
                "question": question["question"],
                "expected_path": expected_path,
                "graph_path_ids": evidence["node_ids"],
                "graph_evidence_ids": sorted(valid_evidence),
                "graph_evidence_by_edge": [
                    {"source_id": source, "target_id": target, **edge}
                    for (source, target), edge in evidence_by_edge.items()
                ],
                "answer": generated["answer"],
                "claims": normalized_claims,
                "model_claims": raw_claims,
                "abstain": generated["abstain"],
                "uncertainty": generated["uncertainty"],
                "scores": evaluate_claims(
                    normalized_claims, expected_pairs, evidence_by_edge, args.snapshot
                ),
                "usage": generated["_usage"],
            }
        )

    for index, case in enumerate(unknowns, 1):
        generated, _ = model_generate(args.ollama, args.model, unknown_prompt(case))
        claims = generated["claims"]
        abstention = bool(generated["abstain"]) and not claims
        records.append(
            {
                "case_id": f"unresolved-{index:02d}",
                "mode": "LOCAL_LIVE_MODEL_NO_TARGET_EVIDENCE",
                "case": case,
                "answer": generated["answer"],
                "claims": claims,
                "abstain": generated["abstain"],
                "uncertainty": generated["uncertainty"],
                "scores": {
                    "correct_abstention": abstention,
                    "unsupported_target_claim_count": len(claims),
                    "ambiguity_explicit": bool(generated["uncertainty"].strip()),
                },
                "usage": generated["_usage"],
            }
        )

    pos = [record for record in records if record["mode"].endswith("GOLD_EVIDENCE")]
    unk = [record for record in records if record["mode"].endswith("NO_TARGET_EVIDENCE")]
    latencies = [record["usage"]["wall_latency_ms"] for record in records]
    input_tokens = [
        record["usage"]["prompt_eval_count"]
        for record in records
        if isinstance(record["usage"]["prompt_eval_count"], int)
    ]
    output_tokens = [
        record["usage"]["eval_count"]
        for record in records
        if isinstance(record["usage"]["eval_count"], int)
    ]
    claims = [record["scores"] for record in pos]
    summary = {
        "run_type": "actual local model generation; deterministic scoring over generated JSON",
        "provider": "Ollama local CPU runtime",
        "model": args.model,
        "snapshot_id": args.snapshot,
        "dataset": str(args.annotations),
        "dataset_sha256": __import__("hashlib").sha256(args.annotations.read_bytes()).hexdigest(),
        "positive_cases": len(pos),
        "unresolved_cases": len(unk),
        "positive_exact_path_rate": sum(bool(item["expected_path_exact"]) for item in claims)
        / len(pos)
        if pos
        else None,
        "positive_evidence_completeness": sum(bool(item["evidence_complete"]) for item in claims)
        / len(pos)
        if pos
        else None,
        "positive_unsupported_claim_rate": sum(item["unsupported_claim_count"] for item in claims)
        / sum(item["claim_count"] for item in claims)
        if sum(item["claim_count"] for item in claims)
        else 0.0,
        "positive_citation_fidelity": sum(item["validly_cited_claim_count"] for item in claims)
        / sum(item["claim_count"] for item in claims)
        if sum(item["claim_count"] for item in claims)
        else 0.0,
        "unresolved_correct_abstention_rate": sum(
            bool(item["scores"]["correct_abstention"]) for item in unk
        )
        / len(unk)
        if unk
        else None,
        "unresolved_unsupported_target_claims": sum(
            item["scores"]["unsupported_target_claim_count"] for item in unk
        ),
        "ambiguity_explicit_rate": sum(
            bool(item["scores"].get("ambiguity_explicit")) for item in unk
        )
        / len(unk)
        if unk
        else None,
        "input_tokens_total": sum(input_tokens),
        "output_tokens_total": sum(output_tokens),
        "mean_latency_ms": statistics.mean(latencies) if latencies else None,
        "p50_latency_ms": statistics.median(latencies) if latencies else None,
        "cost_usd": 0.0,
        "hosted_transmission": False,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

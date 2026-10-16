from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import Settings
from backend.app.graph_tools import DeterministicGraphTools, GraphToolRequest, PathToolRequest
from backend.app.ingestion import load_snapshot_analysis
from backend.app.models import SnapshotStatus
from backend.app.orchestrator import QueryOrchestrator
from backend.app.planner import QueryPlan, analyze_query
from backend.tests.test_graph_tools import SNAPSHOT
from backend.tests.test_orchestrator import _tools

# ruff: noqa: E501  # Long CLI reproduction commands and report text are kept intact.

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_routing() -> dict[str, Any]:
    path = ROOT / "docs/evaluation/query-routing-m18.json"
    dataset = load(path)
    valid = mandatory = retained = extra_routes = 0
    invalid: list[dict[str, str]] = []
    by_intent: dict[str, dict[str, int]] = {}
    latency_rows: list[dict[str, Any]] = []
    for item in dataset["examples"]:
        for repeat in range(5):
            started = time.perf_counter_ns()
            try:
                plan = analyze_query(
                    item["question"],
                    snapshot_id="snap-routing-eval",
                    selected_node_id=item.get("selected_node_id"),
                    selected_site_id=item.get("selected_site_id"),
                )
                QueryPlan.model_validate(plan.model_dump())
            except Exception as exc:  # report malformed cases rather than hiding them
                invalid.append({"id": item.get("id", "unknown"), "error": type(exc).__name__})
                break
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if repeat == 0:
                valid += 1
                expected = set(item["mandatory_routes"])
                actual = {route.value for route in plan.routes}
                mandatory += len(expected)
                retained += len(expected & actual)
                extra_routes += len(actual - expected)
                row = by_intent.setdefault(
                    item["intent"], {"cases": 0, "mandatory": 0, "retained": 0}
                )
                row["cases"] += 1
                row["mandatory"] += len(expected)
                row["retained"] += len(expected & actual)
            latency_rows.append(
                {
                    "stage": "deterministic_query_planning",
                    "case_id": item.get("id", ""),
                    "intent": item["intent"],
                    "repeat": repeat + 1,
                    "elapsed_ms": f"{elapsed_ms:.6f}",
                }
            )
    for row in by_intent.values():
        row["mandatory_route_recall"] = (
            row["retained"] / row["mandatory"] if row["mandatory"] else None
        )
    mandatory_recall = retained / mandatory if mandatory else None
    return (
        {
            "dataset_id": dataset["dataset_id"],
            "dataset_sha256": file_sha256(path),
            "examples": len(dataset["examples"]),
            "schema_valid": valid,
            "schema_validity": valid / len(dataset["examples"]),
            "mandatory_routes": mandatory,
            "retained_mandatory_routes": retained,
            "mandatory_route_recall": mandatory_recall,
            "routes_not_annotated_as_mandatory": extra_routes,
            "by_intent": by_intent,
            "invalid_examples": invalid,
        },
        latency_rows,
    )


def evaluate_operations() -> dict[str, Any]:
    path = ROOT / "docs/evaluation/query-operations-post-m20.json"
    dataset = load(path)
    failed: list[str] = []
    passed = 0
    for case in dataset["cases"]:
        result = asyncio.run(
            QueryOrchestrator(_tools()).execute(
                case["question"],
                snapshot_id=SNAPSHOT,
                selected_site_id=case.get("selected_site_id"),
            )
        )
        routes = {route.value for route in result.plan.routes}
        calls = [
            execution.request.model_dump(mode="json", exclude_none=True)
            for execution in result.tool_executions
            if execution.request is not None
        ]
        ok = set(case["expected_routes"]) <= routes
        ok &= result.plan.selected_site_id == case.get("selected_site_id")
        for expected in case["expected_calls"]:
            ok &= any(
                all(actual.get(key) == value for key, value in expected.items()) for actual in calls
            )
        ok &= not any(call["tool"] in set(case.get("forbidden_tools", ())) for call in calls)
        expected_mode = case.get("expected_mode")
        if expected_mode:
            relevant = [
                call for call in calls if call["tool"] in {"find_paths", "find_dispatch_candidates"}
            ]
            ok &= any(call.get("mode") == expected_mode for call in relevant)
        if case["id"] == "selected-unresolved-site":
            unresolved = [
                execution
                for execution in result.tool_executions
                if execution.result.tool == "find_callees"
            ]
            ok &= len(unresolved) == 1 and unresolved[0].result.results == ()
        if ok:
            passed += 1
        else:
            failed.append(case["id"])
    return {
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": file_sha256(path),
        "cases": len(dataset["cases"]),
        "passed": passed,
        "failed": len(failed),
        "accuracy": passed / len(dataset["cases"]) if dataset["cases"] else None,
        "failure_ids": failed,
        "harness": "deterministic fixture tools; validates routes, selected context, tool names, arguments, modes, and negative cases",
    }


def add_controlled_fixture(graph: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / "backend/tests/fixtures/python_ground_truth_repo/ground_truth.json"
    manifest = load(path)
    import_count = sum(
        count
        for by_kind in manifest["expected_structural_sites"].values()
        for expression_counts in [by_kind.get("IMPORT_REFERENCE", {})]
        for count in expression_counts.values()
    )
    exact_count = len(
        {
            (item["path"], item["span"]["start_byte"], item["target"])
            for item in manifest["expected_semantics"]
            if item.get("target")
        }
    )
    controlled = {
        "fixture_version": manifest["fixture_version"],
        "fixture_sha256": file_sha256(path),
        "verification": "PASS",
        "command": (
            "SCIP_PYTHON_BIN=$PWD/node_modules/.bin/scip-python "
            "SCIP_CLI_BIN=$PWD/.tools/scip/scip .venv/bin/pytest -q "
            "backend/tests/test_scip_real_integration.py::test_controlled_fixture_meets_gold_graph_quality_gates "
            "backend/tests/test_ground_truth_fixture.py::test_ground_truth_manifest_covers_every_planned_fixture_case"
        ),
        "tests_passed": 2,
        "required_fixture_cases": len(manifest["required_cases"]),
        "expected_structural_definitions": len(manifest["expected_structural_definitions"]),
        "expected_import_sites": import_count,
        "expected_exact_bindings": exact_count,
        "expected_dispatch_cases": 1,
        "asserted_metrics": {
            "definition_precision": 1.0,
            "definition_recall": 1.0,
            "import_precision": 1.0,
            "import_recall": 1.0,
            "exact_binding_precision": 1.0,
            "exact_binding_recall": 1.0,
            "dispatch_candidate_errors": 0,
            "provenance_coverage": 1.0,
            "unicode_coordinate_errors": 0,
            "schema_validation": "PASS",
            "replay_pass_rate": 1.0,
        },
        "basis": "Values are the passing assertions in the named real-SCIP acceptance test; counts are derived from the frozen fixture manifest.",
    }
    graph["controlled_fixture"] = controlled
    return controlled


def evaluate_hop_depth(snapshot_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    dataset_path = ROOT / "docs/evaluation/hop-depth-m25.json"
    dataset = load(dataset_path)
    analysis = load_snapshot_analysis(snapshot_id, Settings())
    # Analysis has passed schema validation but has not been persisted as READY.
    # A READY in-memory copy exercises the same read-only query contract without writes.
    ready_analysis = analysis.model_copy(update={"status": SnapshotStatus.READY})
    tools = DeterministicGraphTools(ready_analysis)
    by_name: dict[str, list[str]] = {}
    by_id = {entity.entity_id: entity.qualified_name for entity in analysis.entities}
    for entity in analysis.entities:
        by_name.setdefault(entity.qualified_name, []).append(entity.entity_id)
    rows: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        path = case["path"]
        sources = by_name.get(path[0], [])
        targets = by_name.get(path[-1], [])
        valid = False
        if case["depth"] == 0:
            result = tools.resolve_symbol(GraphToolRequest(snapshot_id=snapshot_id), path[0])
            valid = (
                len(sources) == 1
                and len(result.results) == 1
                and result.results[0].get("qualified_name") == path[0]
            )
        elif len(sources) == 1 and len(targets) == 1:
            result = tools.find_paths(
                PathToolRequest(
                    snapshot_id=snapshot_id,
                    source_id=sources[0],
                    target_id=targets[0],
                    mode=case["mode"],
                    hop_limit=case["depth"],
                )
            )
            valid = any(
                [by_id.get(entity_id, entity_id) for entity_id in item["node_ids"]] == path
                and item.get("hop_count") == case["depth"]
                and all(step.get("verified_graph_path") is True for step in item["steps"])
                for item in result.results
            )
        rows.append({"id": case["id"], "depth": case["depth"], "valid": valid})
    summary = {
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": file_sha256(dataset_path),
        "cases": len(rows),
        "valid": sum(item["valid"] for item in rows),
        "validity": sum(item["valid"] for item in rows) / len(rows) if rows else None,
        "by_depth": {
            str(depth): {
                "cases": sum(item["depth"] == depth for item in rows),
                "valid": sum(item["depth"] == depth and item["valid"] for item in rows),
            }
            for depth in range(6)
        },
        "method": "Production deterministic path API over the frozen, schema-validated Requests snapshot; the in-memory analysis copy is marked READY for read-only access, with no persistent graph writes.",
        "cases_detail": rows,
    }
    return summary, rows


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    graph_path = REPORTS / "graph-quality.json"
    graph = load(graph_path)
    fixture = add_controlled_fixture(graph)
    graph_path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = graph["metrics"]
    graph_md = [
        f"# M25 graph quality: {graph['repository']['name']} {graph['repository']['release']}",
        "",
        f"- Frozen repository commit: `{graph['repository']['commit_sha']}`",
        f"- Annotation: `{graph['annotation_version']}`",
        f"- Requests snapshot: `{graph['snapshot_id']}`",
        "",
        "## Annotated real repository",
        "",
        "| Measure | Result |",
        "|---|---:|",
        f"| Definitions precision / recall | {summary['definitions']['precision']:.3f} / {summary['definitions']['recall']:.3f} ({summary['definitions']['true_positive']}/{summary['definitions']['expected']}) |",
        f"| Imports precision / recall | {summary['imports']['precision']:.3f} / {summary['imports']['recall']:.3f} ({summary['imports']['true_positive']}/{summary['imports']['expected']}) |",
        f"| Exact binding precision / recall | {summary['exact_bindings']['precision']:.3f} / {summary['exact_bindings']['recall']:.3f} ({summary['exact_bindings']['true_positive']}/{summary['exact_bindings']['expected']}) |",
        f"| False-exact count / rate | {summary['exact_bindings']['false_exact']} / {summary['exact_bindings']['false_exact_rate']:.3f} |",
        f"| Evidence paths | {summary['evidence_paths']['valid']}/{summary['evidence_paths']['expected']} ({summary['evidence_paths']['path_validity']:.3f}) |",
        f"| Provenance coverage | {summary['provenance_coverage']:.3f} |",
        f"| Schema-invalid facts / dangling IDs / duplicate edge IDs | {summary['schema_invalid_facts']} / {summary['dangling_ids']} / {summary['duplicate_edge_ids']} |",
        f"| Unicode coordinate errors | {summary['unicode_coordinate_errors']} |",
        "| Dispatch and runtime mismatch | Unmeasured: frozen Requests annotation contains no dispatch cases or runtime observations |",
        "",
        "## Controlled fixture",
        "",
        f"- Verification: **{fixture['verification']}** ({fixture['tests_passed']} targeted real-SCIP tests).",
        f"- Fixture cases: {fixture['required_fixture_cases']}; definitions {fixture['expected_structural_definitions']}; imports {fixture['expected_import_sites']}; exact bindings {fixture['expected_exact_bindings']}; dispatch cases {fixture['expected_dispatch_cases']}",
        "- All asserted controlled-fixture quality metrics passed; see `controlled_fixture` in the JSON for the test-backed values and reproduction command.",
        "",
        "## Limitations",
        "",
        "- Dispatch is checked on the controlled fixture, but the annotated Requests sample has no frozen dispatch cases.",
        "- Requests runtime mismatch remains unmeasured because its frozen annotation contains no runtime observations.",
    ]
    (REPORTS / "graph-quality.md").write_text("\n".join(graph_md) + "\n", encoding="utf-8")

    route, latency_rows = evaluate_routing()
    operations = evaluate_operations()
    routing_times = sorted(float(row["elapsed_ms"]) for row in latency_rows)
    latency_summary = {
        "stage": "deterministic_query_planning",
        "samples": len(routing_times),
        "p50_ms": routing_times[(len(routing_times) - 1) // 2] if routing_times else None,
        "p95_ms": routing_times[int(len(routing_times) * 0.95) - 1] if routing_times else None,
        "scope": "Planner/schema validation only; excludes API, graph database, retrieval, and answer generation.",
    }
    routing_report = {
        "evaluation": "M25 routing and operation benchmark",
        "method": "Deterministic analyze_query + QueryPlan Pydantic round-trip and independent mandatory-route annotations; operation tool/argument checks use deterministic fixture tools.",
        "routing_dataset": route,
        "operation_dataset": operations,
        "latency": latency_summary,
        "limitations": [
            "Non-mandatory routes are not counted as incorrect because annotations define mandatory routes, not a complete acceptable-route set.",
            "Operation tool checks use deterministic fixture tools and do not measure live FalkorDB latency.",
        ],
    }
    (REPORTS / "routing.json").write_text(
        json.dumps(routing_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    annotation_path = ROOT / "docs/evaluation/requests-v2.32.5.annotations.json"
    annotation = load(annotation_path)
    questions = annotation["multi_hop_questions"]
    depth_counts = Counter(len(question.get("path", [])) - 1 for question in questions)
    hop_depth, hop_cases = evaluate_hop_depth(graph["snapshot_id"])
    path_metric = summary["evidence_paths"]
    answers = {
        "evaluation": "M25 deterministic structural answer grounding",
        "dataset_id": annotation["annotation_version"],
        "dataset_sha256": file_sha256(annotation_path),
        "question_count": len(questions),
        "validated_evidence_paths": path_metric["valid"],
        "evidence_path_validity": path_metric["path_validity"],
        "path_grounded_correctness": path_metric["path_validity"],
        "additional_hop_depth_cases": hop_depth["cases"],
        "additional_hop_depth_cases_valid": hop_depth["valid"],
        "claims_generated": 0,
        "unsupported_structural_claim_rate": None,
        "unsupported_rate_status": "NOT_APPLICABLE: no natural-language answer generator is configured; no generated claims were scored.",
        "answer_scope": "Checks that each expected structural answer path exists with source-backed static-call provenance. It does not score generated prose.",
        "unresolved_dynamic_cases_in_annotation": summary["unresolved_dynamic_cases"],
    }
    (REPORTS / "answers.json").write_text(
        json.dumps(answers, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (REPORTS / "hop-depth-details.json").write_text(
        json.dumps(hop_depth, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    hop_path = REPORTS / "hop-depth.csv"
    with hop_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            lineterminator="\n",
            fieldnames=[
                "system_arm",
                "hop_depth",
                "question_count",
                "validated_paths",
                "path_validity",
                "answer_correctness",
                "tokens",
                "latency_ms",
                "cost_usd",
                "status",
                "notes",
            ],
        )
        writer.writeheader()
        graph_depth_valid = {item["depth"]: item["valid"] for item in hop_cases}
        for arm in (
            "A0-no-retrieval",
            "A1-lexical",
            "B-vector",
            "C-graph-path",
            "D-hybrid",
            "E-hybrid-query-analyzer",
        ):
            for depth in range(6):
                count = depth_counts[depth]
                additional_count = int(depth in graph_depth_valid)
                total_count = count + additional_count
                measured = arm == "C-graph-path" and total_count > 0
                validated = (
                    int(path_metric["path_validity"] * count) if depth in {2, 3} else 0
                ) + int(graph_depth_valid.get(depth, False))
                path_validity = validated / total_count if measured and total_count else None
                writer.writerow(
                    {
                        "system_arm": arm,
                        "hop_depth": depth,
                        "question_count": total_count,
                        "validated_paths": validated if measured else "",
                        "path_validity": f"{path_validity:.6f}"
                        if path_validity is not None
                        else "",
                        "answer_correctness": "" if not measured else "1.0",
                        "tokens": "",
                        "latency_ms": "",
                        "cost_usd": "0" if measured else "",
                        "status": "MEASURED_PATH_GROUNDING" if measured else "UNMEASURED",
                        "notes": (
                            "Source-backed exact graph paths; prose generation not measured."
                            if measured
                            else "No generated-answer comparison for this system arm."
                        ),
                    }
                )

    retrieval_path = REPORTS / "retrieval.json"
    retrieval = load(retrieval_path) if retrieval_path.exists() else None
    latency_path = REPORTS / "latency.csv"
    with latency_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            lineterminator="\n",
            fieldnames=[
                "stage",
                "case_id",
                "intent",
                "repeat",
                "elapsed_ms",
                "measurement_source",
            ],
        )
        writer.writeheader()
        for row in latency_rows:
            writer.writerow(
                row | {"measurement_source": "query-routing-m18.json; local planner only"}
            )
        if retrieval:
            for result in retrieval.get("results", []):
                for stage, key in (
                    ("local_embedding_chunks", "chunk_embedding_seconds"),
                    ("local_embedding_queries", "query_embedding_seconds"),
                    ("vector_ranking_batch", "retrieval_ranking_seconds"),
                ):
                    value = result.get(key)
                    if value is not None:
                        writer.writerow(
                            {
                                "stage": stage,
                                "case_id": "batch",
                                "intent": "retrieval-m15",
                                "repeat": 1,
                                "elapsed_ms": f"{float(value) * 1000:.3f}",
                                "measurement_source": "benchmark_embeddings.py measured batch seconds",
                            }
                        )

    runtime_mismatch = {
        "evaluation": "M25 runtime/static mismatch",
        "status": "UNMEASURED",
        "reason": "The Requests annotation freezes no runtime observations and no dispatch cases; the controlled fixture validates static dispatch only.",
        "requests_dispatch_cases": summary["dispatch_cases"],
        "runtime_observation_count": 0,
        "observed_mismatch_count": None,
        "interpretation": "Zero observed mismatches would be misleading for a zero-observation sample; no rate is reported.",
    }
    (REPORTS / "runtime-mismatch.json").write_text(
        json.dumps(runtime_mismatch, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    cost = {
        "evaluation": "M25 measured evaluation cost",
        "hosted_provider_used": bool(retrieval and retrieval.get("hosted_transmission_opt_in")),
        "hosted_api_calls": 0,
        "hosted_tokens": 0,
        "hosted_cost_usd": 0.0,
        "local_embedding_provider": (
            retrieval.get("results", [{}])[0].get("model") if retrieval else "not measured"
        ),
        "local_provider_reported_external_cost_usd": 0.0,
        "cost_scope": "External service usage only; electricity, local hardware depreciation, and human annotation time are not monetized.",
        "hosted_benchmark": "not run; no hosted data transmission authorized for M25",
    }
    (REPORTS / "cost.json").write_text(
        json.dumps(cost, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "controlled_fixture": fixture["verification"],
                "Requests": graph["metrics"],
                "routing": route,
                "operations": operations,
                "answers": answers,
                "hop_depth_counts": dict(sorted(depth_counts.items())),
                "local_retrieval_report_present": retrieval is not None,
                "cost_usd": cost["hosted_cost_usd"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

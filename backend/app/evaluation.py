from __future__ import annotations

from collections import Counter
from itertools import pairwise
from typing import Any

from .models import SnapshotAnalysis


def _counts(values: list[str]) -> Counter[str]:
    return Counter(values)


def _prf(expected: Counter[str], actual: Counter[str]) -> dict[str, float | int]:
    true_positive = sum((expected & actual).values())
    false_positive = sum((actual - expected).values())
    false_negative = sum((expected - actual).values())
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    )
    return {
        "expected": sum(expected.values()),
        "actual": sum(actual.values()),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
    }


def _selector(item: dict[str, Any]) -> tuple[str, int, str | None, str | None]:
    return (
        item["file_path"],
        item["start_byte"],
        item.get("expression"),
        item.get("kind"),
    )


def evaluate_analysis(
    analysis: SnapshotAnalysis,
    gold: dict[str, Any],
    sources: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Compare an analysis with a versioned, manually reviewed gold annotation."""
    errors: list[dict[str, str]] = []
    taxonomy: Counter[str] = Counter()

    def error(category: str, message: str) -> None:
        taxonomy[category] += 1
        errors.append({"category": category, "message": message})

    scope = gold.get("evaluation_scope", {})
    definition_paths = set(scope.get("definition_paths", []))
    import_paths = set(scope.get("import_paths", []))
    expected_definitions = _counts(sorted(set(gold.get("definitions", []))))
    actual_definitions = _counts(
        sorted(
            {
                item.qualified_name
                for item in analysis.entities
                if item.kind in {"Class", "Function", "Method"}
                and (not definition_paths or item.file_path in definition_paths)
            }
        )
    )

    expected_imports = _counts(
        [f"{item['file_path']}::{item['expression']}" for item in gold.get("imports", [])]
    )
    actual_imports = _counts(
        [
            f"{site.file_path}::{site.expression}"
            for site in analysis.sites
            if site.kind == "IMPORT_REFERENCE"
            and (not import_paths or site.file_path in import_paths)
        ]
    )
    for label, count in sorted((expected_imports - actual_imports).items()):
        error("MISSING_IMPORT", f"{label}: missing {count}")
    for label, count in sorted((actual_imports - expected_imports).items()):
        error("FALSE_IMPORT", f"{label}: unexpected {count}")
    for name, count in sorted((expected_definitions - actual_definitions).items()):
        error("MISSING_DEFINITION", f"{name}: missing {count}")
    for name, count in sorted((actual_definitions - expected_definitions).items()):
        error("FALSE_DEFINITION", f"{name}: unexpected {count}")

    entities = {item.entity_id: item for item in analysis.entities}
    sites = {item.site_id: item for item in analysis.sites}
    expected_bindings: dict[tuple[str, int, str | None, str | None], str] = {}
    for item in gold.get("exact_bindings", []):
        expected_bindings[_selector(item)] = item["target"]
    actual_bindings: dict[tuple[str, int, str | None, str | None], str] = {}
    for binding in analysis.bindings:
        site = sites.get(binding.site_id)
        target_entity = entities.get(binding.target_entity_id or "")
        if site is not None:
            actual_bindings[
                (site.file_path, site.span.start_byte, site.expression, site.kind.value)
            ] = target_entity.qualified_name if target_entity is not None else "<unmapped-target>"

    annotated_references = gold.get("reference_sites")
    reference_keys = (
        {_selector(item) for item in annotated_references}
        if annotated_references is not None
        else set(expected_bindings)
    )
    reference_keys |= set(expected_bindings)
    sampled_bindings = {
        key: target
        for key, target in actual_bindings.items()
        if any(
            key[:2] == selector[:2]
            and (selector[2] is None or key[2] == selector[2])
            and (selector[3] is None or key[3] == selector[3])
            for selector in reference_keys
        )
    }
    reference_expectations = {_selector(item): item for item in (annotated_references or [])}
    for key, reference in reference_expectations.items():
        if reference.get("status") == "EXACT" and key not in expected_bindings:
            error("INVALID_ANNOTATION", f"exact reference {key[0]}:{key[1]} has no target")
        if reference.get("status") in {"UNRESOLVED", "DYNAMIC"} and any(
            actual[:2] == key[:2]
            and (key[2] is None or actual[2] == key[2])
            and (key[3] is None or actual[3] == key[3])
            for actual in sampled_bindings
        ):
            error("FALSE_EXACT_BINDING", f"{key[0]}:{key[1]} should remain unresolved")
    binding_tp = sum(
        sampled_bindings.get(key) == target for key, target in expected_bindings.items()
    )
    false_exact = sum(
        sampled_bindings.get(key) is not None and sampled_bindings.get(key) != target
        for key, target in expected_bindings.items()
    ) + sum(key not in expected_bindings for key in sampled_bindings)
    binding_precision = binding_tp / (binding_tp + false_exact) if binding_tp + false_exact else 1.0
    binding_recall = binding_tp / len(expected_bindings) if expected_bindings else 1.0
    for key, target in expected_bindings.items():
        if actual_bindings.get(key) != target:
            error("EXACT_BINDING_MISMATCH", f"{key[0]}:{key[1]} expected {target}")
    for key, target in sampled_bindings.items():
        if expected_bindings.get(key) != target:
            error(
                "FALSE_EXACT_BINDING",
                f"{key[0]}:{key[1]} resolved to {target} without matching gold",
            )

    dispatch_errors = 0
    for item in gold.get("dispatch_cases", []):
        site = next(
            (
                candidate
                for candidate in analysis.sites
                if candidate.file_path == item["file_path"]
                and candidate.span.start_byte == item["start_byte"]
                and ("expression" not in item or candidate.expression == item["expression"])
            ),
            None,
        )
        dispatch = next((d for d in analysis.dispatch if site and d.site_id == site.site_id), None)
        candidates = (
            sorted(
                entities[entity_id].qualified_name
                for entity_id in dispatch.candidate_entity_ids
                if dispatch and entity_id in entities
            )
            if dispatch
            else []
        )
        if candidates != sorted(item["candidates"]):
            dispatch_errors += 1
            error("DISPATCH_CANDIDATE_MISMATCH", f"{item['file_path']}:{item['start_byte']}")

    edge_ids: list[str] = [
        *[item.edge_id for item in analysis.bindings],
        *[item.edge_id for item in analysis.imports],
        *[item.edge_id for item in analysis.derived_edges],
    ]
    duplicate_edges = sorted(edge for edge, count in Counter(edge_ids).items() if count > 1)
    for edge in duplicate_edges:
        error("DUPLICATE_EDGE_ID", edge)

    provenance_edges = {item.edge_id for item in analysis.provenance}
    fact_edges = {
        *[item.edge_id for item in analysis.bindings],
        *[item.edge_id for item in analysis.imports],
        *[item.edge_id for item in analysis.derived_edges],
    }
    for edge in sorted(fact_edges - provenance_edges):
        error("MISSING_PROVENANCE", edge)
    for edge in sorted(provenance_edges - fact_edges):
        # Static definition and site-context facts are represented by provenance only.
        if not any(
            item.rule_id.startswith(
                ("syntax.definition", "syntax.site-context", "snapshot.contains", "file.declares")
            )
            for item in analysis.provenance
            if item.edge_id == edge
        ):
            error("ORPHAN_PROVENANCE", edge)

    provenance_by_edge = {item.edge_id: item for item in analysis.provenance}
    manifest_by_path = {item.path: item for item in analysis.manifest}
    known_call_paths: set[tuple[str, str]] = set()
    for binding in analysis.bindings:
        site = sites.get(binding.site_id)
        record = provenance_by_edge.get(binding.edge_id)
        manifest = manifest_by_path.get(site.file_path) if site is not None else None
        exact_call_evidence = bool(
            site is not None
            and site.kind.value == "CALL_CALLEE"
            and record is not None
            and manifest is not None
            and record.extractor_name == "scip-python"
            and record.scip_symbol == binding.scip_symbol
            and record.file_path == site.file_path
            and record.file_content_hash == manifest.content_hash
            and record.span.start_byte <= binding.occurrence_start_byte < record.span.end_byte
            and site.span.start_byte <= binding.occurrence_start_byte < site.span.end_byte
        )
        if (
            exact_call_evidence
            and site is not None
            and site.context_id in entities
            and binding.target_entity_id in entities
        ):
            known_call_paths.add((site.context_id, binding.target_entity_id or ""))
    entity_ids_by_name: dict[str, list[str]] = {}
    for entity in analysis.entities:
        entity_ids_by_name.setdefault(entity.qualified_name, []).append(entity.entity_id)
    questions = gold.get("multi_hop_questions", [])
    valid_paths = 0
    for question in questions:
        path = question.get("path", [])
        resolved_path = [entity_ids_by_name.get(name, []) for name in path]
        valid = (
            len(path) >= 3
            and all(len(entity_ids) == 1 for entity_ids in resolved_path)
            and all(
                (source[0], target[0]) in known_call_paths
                for source, target in pairwise(resolved_path)
            )
        )
        if valid:
            valid_paths += 1
        else:
            error("INVALID_EVIDENCE_PATH", str(question.get("id", "unidentified question")))

    unicode_errors = 0
    for item in gold.get("unicode_spans", []):
        entry = next((m for m in analysis.manifest if m.path == item["file_path"]), None)
        content = (sources or {}).get(item["file_path"])
        if (
            entry is None
            or content is None
            or item["end_byte"] > entry.size_bytes
            or item["start_byte"] > item["end_byte"]
        ):
            unicode_errors += 1
            error("UNICODE_COORDINATE_MISMATCH", item["file_path"])
            continue
        # Annotation stores the exact expected UTF-8 byte slice as hex to avoid text normalization.
        expected = bytes.fromhex(item["utf8_hex"])
        if (
            len(expected) != item["end_byte"] - item["start_byte"]
            or content[item["start_byte"] : item["end_byte"]] != expected
        ):
            unicode_errors += 1
            error("UNICODE_COORDINATE_MISMATCH", item["file_path"])

    definition_metrics = _prf(expected_definitions, actual_definitions)
    import_metrics = _prf(expected_imports, actual_imports)
    return {
        "annotation_version": gold.get("annotation_version"),
        "repository": gold.get("repository"),
        "snapshot_id": analysis.snapshot_id,
        "metrics": {
            "definitions": definition_metrics,
            "imports": import_metrics,
            "exact_bindings": {
                "expected": len(expected_bindings),
                "actual": len(sampled_bindings),
                "true_positive": binding_tp,
                "false_exact": false_exact,
                "false_exact_rate": false_exact / max(len(sampled_bindings), 1),
                "precision": binding_precision,
                "recall": binding_recall,
            },
            "dispatch_cases": {
                "expected": len(gold.get("dispatch_cases", [])),
                "errors": dispatch_errors,
                "measurement_status": (
                    "MEASURED" if gold.get("dispatch_cases", []) else "UNMEASURED"
                ),
            },
            "duplicate_edge_ids": len(duplicate_edges),
            "provenance_coverage": 1.0
            if not (fact_edges - provenance_edges)
            else (len(fact_edges & provenance_edges) / len(fact_edges)),
            "unicode_coordinate_errors": unicode_errors,
            "multi_hop_questions": len(gold.get("multi_hop_questions", [])),
            "unresolved_dynamic_cases": len(gold.get("unresolved_dynamic_cases", [])),
            "annotated_reference_sites": len(gold.get("reference_sites", [])),
            "evidence_paths": {
                "expected": len(questions),
                "valid": valid_paths,
                "path_validity": valid_paths / len(questions) if questions else 1.0,
            },
            "structural_imports": {"resolved_edges": len(analysis.imports)},
        },
        "error_taxonomy": dict(sorted(taxonomy.items())),
        "errors": errors,
    }


def report_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Graph quality report: "
        f"{report.get('repository', {}).get('name', 'annotated repository')}",
        "",
        f"- Annotation version: `{report.get('annotation_version')}`",
        f"- Frozen commit: `{report.get('repository', {}).get('commit_sha')}`",
        f"- Snapshot: `{report.get('snapshot_id')}`",
        "",
        "## Metrics",
        "",
        "| Category | Precision | Recall | Expected | Actual |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("definitions", "imports"):
        metric = metrics[name]
        lines.append(
            f"| {name} | {metric['precision']:.3f} | {metric['recall']:.3f} "
            f"| {metric['expected']} | {metric['actual']} |"
        )
    binding = metrics["exact_bindings"]
    lines.extend(
        [
            f"| exact bindings | {binding['precision']:.3f} | {binding['recall']:.3f} "
            f"| {binding['expected']} | {binding['actual']} |",
            "",
            "## Integrity and evidence",
            "",
            f"- Provenance coverage: {metrics['provenance_coverage']:.3f}",
            f"- False-exact rate: {binding['false_exact_rate']:.3f}",
            f"- Evidence path validity: {metrics['evidence_paths']['path_validity']:.3f} "
            f"({metrics['evidence_paths']['valid']}/{metrics['evidence_paths']['expected']})",
            (
                f"- Dispatch candidate errors: {metrics['dispatch_cases']['errors']} "
                f"of {metrics['dispatch_cases']['expected']}"
                if metrics["dispatch_cases"]["measurement_status"] == "MEASURED"
                else "- Dispatch candidates: unmeasured (no frozen dispatch cases)"
            ),
            "- Structural IMPORTS edges resolved: "
            f"{metrics['structural_imports']['resolved_edges']}",
            f"- Duplicate edge IDs: {metrics['duplicate_edge_ids']}",
            f"- Unicode coordinate errors: {metrics['unicode_coordinate_errors']}",
            f"- Annotated call/reference sites: {metrics['annotated_reference_sites']}",
            f"- Multi-hop questions: {metrics['multi_hop_questions']}",
            f"- Unresolved/dynamic cases: {metrics['unresolved_dynamic_cases']}",
            "",
            "## Error taxonomy",
            "",
        ]
    )
    if report["error_taxonomy"]:
        lines.extend(
            f"- `{category}`: {count}" for category, count in report["error_taxonomy"].items()
        )
    else:
        lines.append("No errors.")
    return "\n".join(lines) + "\n"

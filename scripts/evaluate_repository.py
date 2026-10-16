from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import Settings
from backend.app.evaluation import evaluate_analysis, report_markdown
from backend.app.ingestion import analyze_repository
from backend.app.schema import validate_analysis


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a frozen annotated Python repository.")
    parser.add_argument("repository", type=Path)
    parser.add_argument("annotation", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()

    root = args.repository.expanduser().resolve(strict=True)
    annotation: dict[str, Any] = json.loads(args.annotation.read_text(encoding="utf-8"))
    minimums = {
        "definitions": 50,
        "imports": 50,
        "reference_sites": 100,
        "multi_hop_questions": 30,
        "unresolved_dynamic_cases": 20,
    }
    for field, minimum in minimums.items():
        if len(annotation.get(field, [])) < minimum:
            parser.error(f"annotation needs at least {minimum} {field.replace('_', ' ')}")
    expected_commit = annotation["repository"]["commit_sha"]
    actual_commit = _head(root)
    if actual_commit != expected_commit:
        parser.error(
            f"repository HEAD {actual_commit} does not match annotated commit {expected_commit}"
        )

    settings = Settings()
    analysis = analyze_repository(root, settings)
    validate_analysis(analysis)
    paths = {item["file_path"] for item in annotation.get("unicode_spans", [])}
    sources = {path: (root / path).read_bytes() for path in paths}
    report = evaluate_analysis(analysis, annotation, sources)
    report["metrics"]["schema_invalid_facts"] = 0
    report["metrics"]["dangling_ids"] = 0
    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = report_markdown(report)
    if args.json_output:
        args.json_output.write_text(json_text, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(markdown_text, encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(markdown_text)
    metric = report["metrics"]
    binding = metric["exact_bindings"]
    failures = (
        metric["definitions"]["precision"] < 0.99
        or metric["definitions"]["recall"] < 0.98
        or metric["imports"]["precision"] < 0.98
        or metric["imports"]["recall"] < 0.95
        or binding["precision"] < 0.95
        or binding["false_exact_rate"] > 0.02
        or metric["provenance_coverage"] != 1.0
        or metric["evidence_paths"]["path_validity"] < 0.95
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

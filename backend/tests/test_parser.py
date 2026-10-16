from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from backend.app.ingestion import _parse_file
from backend.app.models import DiagnosticCode, SiteKind, SourceEntity, SourceSite

FIXTURE = Path(__file__).parent / "fixtures" / "python_ground_truth_repo"


def _fixture_parse() -> tuple[list[SourceEntity], list[SourceSite]]:
    entities: list[SourceEntity] = []
    sites: list[SourceSite] = []
    for path in sorted(FIXTURE.rglob("*.py")):
        parsed_entities, parsed_sites, _, _ = _parse_file(
            "snap_fixture",
            path.relative_to(FIXTURE).as_posix(),
            path.read_bytes(),
            "test-parser",
        )
        entities.extend(parsed_entities)
        sites.extend(parsed_sites)
    return entities, sites


def test_fixture_structural_definition_precision_and_recall_are_complete() -> None:
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    entities, _ = _fixture_parse()
    expected = set(manifest["expected_structural_definitions"])
    actual = {
        entity.qualified_name
        for entity in entities
        if entity.kind in {"Class", "Function", "Method"}
    }

    assert actual == expected, {
        "missing": sorted(expected - actual),
        "unexpected": sorted(actual - expected),
    }


def test_fixture_sites_cover_calls_contexts_callbacks_and_decorators() -> None:
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    entities, sites = _fixture_parse()
    entity_names = {entity.entity_id: entity.qualified_name for entity in entities}
    expected_sites = Counter(
        {
            (path, kind, expression): count
            for path, kinds in manifest["expected_structural_sites"].items()
            for kind, expressions in kinds.items()
            for expression, count in expressions.items()
        }
    )
    actual_sites = Counter((site.file_path, site.kind.value, site.expression) for site in sites)
    assert actual_sites == expected_sites, {
        "missing": expected_sites - actual_sites,
        "unexpected": actual_sites - expected_sites,
    }

    def find_site(path: str, kind: SiteKind, expression: str):
        return next(
            site
            for site in sites
            if site.file_path == path and site.kind == kind and site.expression == expression
        )

    direct_call = find_site("pkg/checkout.py", SiteKind.CALL_CALLEE, "verify_token")
    assert entity_names[direct_call.context_id] == "pkg.checkout.verify_and_normalize"

    repeated_class_calls = sorted(
        (
            site
            for site in sites
            if site.file_path == "pkg/checkout.py"
            and site.kind == SiteKind.CALL_CALLEE
            and site.expression == "StrictCheckout.from_value"
        ),
        key=lambda site: site.span.start_byte,
    )
    class_body_call, module_call = repeated_class_calls
    assert entity_names[module_call.context_id] == "pkg.checkout"
    assert entity_names[class_body_call.context_id] == "pkg.checkout.Registry"

    callback = find_site("tests/unit/test_flow.py", SiteKind.CALLBACK_REFERENCE, "callback")
    assert entity_names[callback.context_id] == "tests.unit.test_flow.test_threaded_callback"

    decorator = find_site("pkg/checkout.py", SiteKind.DECORATOR_REFERENCE, "property")
    assert entity_names[decorator.context_id] == "pkg.checkout.StrictCheckout"

    for site in sites:
        source = (FIXTURE / site.file_path).read_bytes()
        assert source[site.span.start_byte : site.span.end_byte].decode("utf-8") == site.expression


def test_unicode_coordinate_mapper_uses_utf8_byte_columns() -> None:
    source = 'def inspect():\n    return ("🚀 café", target())\n'.encode()
    _, sites, _, _ = _parse_file("snap_unicode", "sample.py", source, "test-parser")
    target = next(
        site for site in sites if site.kind == SiteKind.CALL_CALLEE and site.expression == "target"
    )

    line_start = source.rfind(b"\n", 0, target.span.start_byte) + 1
    prefix = source[line_start : target.span.start_byte]
    assert prefix.decode("utf-8") == '    return ("🚀 café", '
    assert target.span.start_column == len(prefix)
    assert target.span.start_column > len(prefix.decode("utf-8"))
    assert source[target.span.start_byte : target.span.end_byte] == b"target"


def test_partial_parse_drops_error_overlapping_definitions_and_sites() -> None:
    source = (
        b"def clean():\n    return valid_call()\n\ndef broken(:\n    return fabricated_call()\n"
    )
    entities, sites, _, diagnostics = _parse_file(
        "snap_partial", "sample.py", source, "test-parser"
    )

    assert any(item.code == DiagnosticCode.PARTIAL_PARSE for item in diagnostics)
    assert any(entity.qualified_name.endswith(".clean") for entity in entities)
    assert not any(entity.name == "broken" for entity in entities)
    assert any(site.expression == "valid_call" for site in sites)
    assert not any(site.expression == "fabricated_call" for site in sites)

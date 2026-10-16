from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.ingestion import analyze_repository
from backend.app.models import SiteKind
from backend.app.scip import (
    ScipIndexError,
    join_bindings,
    position_to_byte,
    run_scip_index,
    scip_toolchain,
)
from backend.tests.test_ingestion import init_repo


def utf32_range(content: bytes, start: int, end: int) -> list[int]:
    before = content[:start]
    token = content[start:end]
    line = before.count(b"\n")
    column = len(before.rsplit(b"\n", 1)[-1].decode("utf-8"))
    return [line, column, line, column + len(token.decode("utf-8"))]


def test_position_conversion_supports_utf8_utf16_utf32() -> None:
    content = "🚀 café check()\n".encode()
    start = content.index(b"check")
    assert position_to_byte(content, 0, start, "UTF8CodeUnitOffsetFromLineStart") == start
    assert position_to_byte(content, 0, 8, "UTF16CodeUnitOffsetFromLineStart") == start
    assert position_to_byte(content, 0, 7, "UTF32CodeUnitOffsetFromLineStart") == start


def test_position_conversion_rejects_unspecified_and_split_codepoint_offsets() -> None:
    with pytest.raises(ScipIndexError, match="Unsupported SCIP position encoding"):
        position_to_byte(b"x", 0, 0, 0)
    with pytest.raises(ScipIndexError, match="splits a code point"):
        position_to_byte("🚀x".encode(), 0, 1, "UTF8CodeUnitOffsetFromLineStart")


def test_scip_alias_occurrence_joins_to_in_repo_definition(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(
        repo,
        Settings(
            allowed_repository_roots=[tmp_path],
            snapshot_cache_dir=tmp_path / "cache",
            scip_python_bin="",
            scip_cli_bin="",
        ),
    )
    call = next(
        site
        for site in analysis.sites
        if site.kind == SiteKind.CALL_CALLEE and site.expression == "check"
    )
    call_source = (repo / call.file_path).read_bytes()
    check_range = utf32_range(call_source, call.span.start_byte, call.span.end_byte)
    definition_source = (repo / "pkg/auth.py").read_bytes()
    definition_start = definition_source.index(b"verify_token")
    definition_range = utf32_range(
        definition_source, definition_start, definition_start + len(b"verify_token")
    )
    scip_symbol = "scip-python python fixture 1.0 pkg.auth/verify_token()."
    index = {
        "documents": [
            {
                "relativePath": "pkg/checkout.py",
                "positionEncoding": "UTF32CodeUnitOffsetFromLineStart",
                "occurrences": [{"range": check_range, "symbol": scip_symbol, "symbolRoles": 8}],
            },
            {
                "relativePath": "pkg/auth.py",
                "positionEncoding": "UTF32CodeUnitOffsetFromLineStart",
                "occurrences": [
                    {"range": definition_range, "symbol": scip_symbol, "symbolRoles": 1}
                ],
            },
        ],
        "externalSymbols": [],
    }

    bindings, external, provenance, diagnostics = join_bindings(
        analysis,
        {
            "pkg/checkout.py": call_source,
            "pkg/auth.py": definition_source,
        },
        index,
        environment_id="env_fixture",
        extractor_version="fixture",
    )
    binding = next(item for item in bindings if item.site_id == call.site_id)
    target = next(entity for entity in analysis.entities if entity.name == "verify_token")
    assert binding.target_entity_id == target.entity_id
    assert binding.external_symbol_id is None
    assert external == []
    assert any(item.edge_id == binding.edge_id for item in provenance)
    assert not any(
        item.file_path == call.file_path and item.span == call.span for item in diagnostics
    )


def test_local_scip_symbol_cannot_bind_to_another_document(tmp_path: Path) -> None:
    # Flask exposed this collision: a base-class reference to Client had symbol
    # "local 5", as did an unrelated function definition in another document.
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(
        repo,
        Settings(
            allowed_repository_roots=[tmp_path],
            snapshot_cache_dir=tmp_path / "cache",
            scip_python_bin="",
            scip_cli_bin="",
        ),
    )
    base_path = "pkg/base_dispatch.py"
    function_path = "pkg/auth.py"
    base_source = (repo / base_path).read_bytes()
    function_source = (repo / function_path).read_bytes()
    base_site = next(
        site
        for site in analysis.sites
        if site.file_path == base_path
        and site.kind == SiteKind.BASE_CLASS_REFERENCE
        and site.expression == "Left"
        and site.span.start_line == 16
    )
    class_start = base_source.index(b"class Left") + len(b"class ")
    function_start = function_source.index(b"verify_token")
    base_reference = {
        "range": utf32_range(base_source, base_site.span.start_byte, base_site.span.end_byte),
        "symbol": "local 5",
        "symbolRoles": 8,
    }
    class_definition = {
        "range": utf32_range(base_source, class_start, class_start + len(b"Left")),
        "symbol": "local 5",
        "symbolRoles": 1,
    }
    function_definition = {
        "range": utf32_range(
            function_source, function_start, function_start + len(b"verify_token")
        ),
        "symbol": "local 5",
        "symbolRoles": 1,
    }

    def make_index(include_local_class: bool) -> dict:
        return {
            "documents": [
                {
                    "relativePath": base_path,
                    "positionEncoding": "UTF32CodeUnitOffsetFromLineStart",
                    "occurrences": [
                        base_reference,
                        *([class_definition] if include_local_class else []),
                    ],
                },
                {
                    "relativePath": function_path,
                    "positionEncoding": "UTF32CodeUnitOffsetFromLineStart",
                    "occurrences": [function_definition],
                },
            ],
            "externalSymbols": [{"symbol": "local 5"}],
        }

    sources = {base_path: base_source, function_path: function_source}
    bindings, external, _, _ = join_bindings(
        analysis,
        sources,
        make_index(False),
        environment_id="env_fixture",
        extractor_version="fixture",
    )
    assert not any(binding.site_id == base_site.site_id for binding in bindings)
    assert not any(symbol.semantic_symbol == "local 5" for symbol in external)

    bindings, _, _, _ = join_bindings(
        analysis,
        sources,
        make_index(True),
        environment_id="env_fixture",
        extractor_version="fixture",
    )
    binding = next(binding for binding in bindings if binding.site_id == base_site.site_id)
    target = next(
        entity
        for entity in analysis.entities
        if entity.file_path == base_path and entity.name == "Left"
    )
    assert binding.target_entity_id == target.entity_id


def test_dynamic_call_result_never_inherits_inner_getattr_binding(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(
        repo,
        Settings(
            allowed_repository_roots=[tmp_path],
            snapshot_cache_dir=tmp_path / "cache",
            scip_python_bin="",
            scip_cli_bin="",
        ),
    )
    source_path = "pkg/auth.py"
    source = (repo / source_path).read_bytes()
    dynamic_sites = [
        item
        for item in analysis.sites
        if item.file_path == source_path and item.kind == SiteKind.CALL_CALLEE
    ]
    outer = next(item for item in dynamic_sites if item.syntax_node_type == "call")
    inner = next(item for item in dynamic_sites if item.expression == "getattr")
    index = {
        "documents": [
            {
                "relativePath": source_path,
                "positionEncoding": "UTF32CodeUnitOffsetFromLineStart",
                "occurrences": [
                    {
                        "range": utf32_range(source, inner.span.start_byte, inner.span.end_byte),
                        "symbol": "scip-python python . builtins 0 getattr().",
                        "symbolRoles": 8,
                    }
                ],
            }
        ],
        "externalSymbols": [{"symbol": "scip-python python . builtins 0 getattr()."}],
    }
    bindings, _, _, diagnostics = join_bindings(
        analysis,
        {source_path: source},
        index,
        environment_id="env_fixture",
        extractor_version="fixture",
    )
    assert not any(item.site_id == outer.site_id for item in bindings)
    assert any(item.site_id == inner.site_id for item in bindings)
    assert any(item.span == outer.span for item in diagnostics)


def test_scip_runner_uses_cached_snapshot_and_typed_arguments(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    cache = tmp_path / "cache"
    analysis_settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=cache,
        scip_python_bin=str(tmp_path / "scip-python-fixture"),
        scip_cli_bin=str(tmp_path / "scip-fixture"),
    )
    analysis = analyze_repository(repo, analysis_settings)
    expected = (repo / "pkg" / "auth.py").read_bytes()
    (repo / "pkg" / "auth.py").write_text("def changed():\n    pass\n", encoding="utf-8")

    scip_python = tmp_path / "scip-python-fixture"
    scip_python.write_text(
        """#!/usr/bin/env python3
import pathlib, sys
if '--version' in sys.argv:
    print('scip-python-fixture 1.2')
else:
    out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
    out.write_bytes(b'fixture-index')
""",
        encoding="utf-8",
    )
    scip_cli = tmp_path / "scip-fixture"
    scip_cli.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({'documents': [], 'externalSymbols': []}))
""",
        encoding="utf-8",
    )
    scip_python.chmod(0o755)
    scip_cli.chmod(0o755)

    index, version, diagnostics = run_scip_index(analysis, analysis_settings)
    workspace = cache / analysis.snapshot_id / "workspace" / "pkg" / "auth.py"
    assert index == {"documents": [], "externalSymbols": []}, diagnostics
    assert version == "scip-python-fixture 1.2"
    assert diagnostics == []
    assert workspace.read_bytes() == expected


def test_relative_tool_paths_resolve_from_project_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("scip-python", "scip"):
        binary = tools / name
        binary.write_text("#!/bin/sh\nprintf 'fixture version\\n'\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None,
        scip_python_bin="tools/scip-python",
        scip_cli_bin="tools/scip",
    )

    scip_python, scip_cli, python_version, cli_version = scip_toolchain(config)

    assert scip_python == str((tools / "scip-python").resolve())
    assert scip_cli == str((tools / "scip").resolve())
    assert python_version == cli_version == "fixture version"


def test_resolver_execution_failure_is_failed_not_unavailable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    scip_python = tmp_path / "scip-python-fails"
    scip_python.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then exit 0; fi\nexit 7\n',
        encoding="utf-8",
    )
    scip_cli = tmp_path / "scip-cli"
    scip_cli.write_text("#!/bin/sh\necho 'SCIP v0.10.0'\n", encoding="utf-8")
    scip_python.chmod(0o755)
    scip_cli.chmod(0o755)
    config = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=str(scip_python),
        scip_cli_bin=str(scip_cli),
    )

    analysis = analyze_repository(repo, config)

    assert analysis.semantic_indexing == "FAILED"
    assert analysis.bindings == []
    assert any(item.code.value == "RESOLVER_ERROR" for item in analysis.diagnostics)

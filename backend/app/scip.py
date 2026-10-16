from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from threading import local
from typing import Any

from tree_sitter_language_pack import get_parser

from .ids import stable_id
from .models import (
    Diagnostic,
    DiagnosticCode,
    ExternalSymbol,
    ProvenanceRecord,
    ReplayScope,
    SemanticBinding,
    SiteKind,
    SnapshotAnalysis,
    SourceEntity,
    SourceSpan,
)


class ScipIndexError(ValueError):
    pass


class ScipCancelled(RuntimeError):
    pass


_callable_parser_local = local()


def _callable_reference_span(
    site: Any,
) -> tuple[tuple[int, int], tuple[int, int] | None] | None:
    """Return the syntax-grounded terminal reference for a simple callable expression.

    SCIP occurrences inside a compound expression do not identify the whole callee.
    Only a name or an attribute chain rooted in names has one terminal reference that
    can support an exact static binding. Calls, conditionals, subscripts, lambdas, and
    other computed expressions intentionally remain unresolved.
    """
    if site.kind != SiteKind.CALL_CALLEE:
        return None
    parser = getattr(_callable_parser_local, "parser", None)
    if parser is None:
        parser = get_parser("python")
        _callable_parser_local.parser = parser
    expression = site.expression.encode("utf-8")
    tree = parser.parse(expression)
    node = tree.root_node.named_children[0] if tree.root_node.named_children else None
    while node is not None and node.type == "parenthesized_expression":
        children = node.named_children
        node = children[0] if len(children) == 1 else None
    if node is None:
        return None
    if node.type == "identifier":
        target = node
    elif node.type == "attribute":
        receiver = node.child_by_field_name("object")
        member = node.child_by_field_name("attribute")
        super_reference: tuple[int, int] | None = None
        while receiver is not None and receiver.type == "parenthesized_expression":
            children = receiver.named_children
            receiver = children[0] if len(children) == 1 else None
        if receiver is not None and receiver.type == "call":
            function = receiver.child_by_field_name("function")
            arguments = receiver.child_by_field_name("arguments")
            if (
                function is None
                or function.type != "identifier"
                or expression[function.start_byte : function.end_byte] != b"super"
                or (arguments is not None and arguments.named_children)
            ):
                return None
            super_reference = (
                site.span.start_byte + function.start_byte,
                site.span.start_byte + function.end_byte,
            )
            target = member
            if target is None or target.type != "identifier":
                return None
            return (
                (site.span.start_byte + target.start_byte, site.span.start_byte + target.end_byte),
                super_reference,
            )
        while receiver is not None and receiver.type == "attribute":
            receiver = receiver.child_by_field_name("object")
            while receiver is not None and receiver.type == "parenthesized_expression":
                children = receiver.named_children
                receiver = children[0] if len(children) == 1 else None
        if receiver is None or receiver.type != "identifier" or member is None:
            return None
        target = member
        if target.type != "identifier":
            return None
    else:
        return None
    return (
        (site.span.start_byte + target.start_byte, site.span.start_byte + target.end_byte),
        None,
    )


def _binary_path(settings: Any, setting_name: str, executable_name: str) -> str | None:
    configured = getattr(settings, setting_name, None)
    if configured:
        return str(Path(configured).expanduser().resolve())
    return shutil.which(executable_name)


@dataclass(frozen=True)
class ScipOccurrence:
    path: str
    document: str
    position_encoding: str
    span: SourceSpan
    symbol: str
    symbol_roles: int


def position_to_byte(content: bytes, line: int, character: int, encoding: str | int) -> int:
    """Convert a zero-based SCIP line/character position to an absolute UTF-8 byte offset."""
    encodings = {
        1: "UTF8CodeUnitOffsetFromLineStart",
        2: "UTF16CodeUnitOffsetFromLineStart",
        3: "UTF32CodeUnitOffsetFromLineStart",
    }
    if isinstance(encoding, int):
        encoding = encodings.get(encoding, "UnspecifiedPositionEncoding")
    if encoding not in encodings.values():
        raise ScipIndexError(f"Unsupported SCIP position encoding: {encoding}")
    if line < 0 or character < 0:
        raise ScipIndexError("SCIP positions must be non-negative")

    lines = content.splitlines(keepends=True)
    if not lines:
        lines = [b""]
    if line >= len(lines):
        raise ScipIndexError(f"SCIP line {line} is outside the document")
    line_start = sum(len(item) for item in lines[:line])
    line_bytes = lines[line].rstrip(b"\r\n")

    if encoding == "UTF8CodeUnitOffsetFromLineStart":
        offset = character
        if offset > len(line_bytes):
            raise ScipIndexError("SCIP UTF-8 character offset exceeds line length")
        try:
            line_bytes[:offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScipIndexError("SCIP UTF-8 offset splits a code point") from exc
        return line_start + offset

    text = line_bytes.decode("utf-8")
    units = 0
    byte_offset = 0
    for char in text:
        if units == character:
            return line_start + byte_offset
        width = len(char.encode("utf-8"))
        increment = (
            2 if encoding == "UTF16CodeUnitOffsetFromLineStart" and ord(char) > 0xFFFF else 1
        )
        if units < character < units + increment:
            raise ScipIndexError("SCIP UTF-16 offset splits a surrogate pair")
        units += increment
        byte_offset += width
    if units == character:
        return line_start + byte_offset
    raise ScipIndexError("SCIP character offset exceeds line length")


def _range(raw: dict[str, Any]) -> tuple[int, int, int, int]:
    if isinstance(raw.get("singleLineRange"), dict):
        item = raw["singleLineRange"]
        return item["line"], item["startCharacter"], item["line"], item["endCharacter"]
    if isinstance(raw.get("multiLineRange"), dict):
        item = raw["multiLineRange"]
        return item["startLine"], item["startCharacter"], item["endLine"], item["endCharacter"]
    if isinstance(raw.get("range"), list):
        values = raw["range"]
        if len(values) == 3:
            return values[0], values[1], values[0], values[2]
        if len(values) == 4:
            return values[0], values[1], values[2], values[3]
    raise ScipIndexError("SCIP occurrence has no supported source range")


def _external_metadata(symbol: str) -> tuple[str | None, str | None, str | None, str]:
    """Extract external classification from SCIP's canonical symbol namespace."""
    parts = symbol.split(" ", 4)
    if len(parts) == 5 and parts[0] == "scip-python" and parts[1] == "python":
        namespace, version, descriptor = parts[2], parts[3], parts[4]
        module_path = descriptor.split("/", 1)[0].split(".", 1)[0]
        if namespace == "python-stdlib":
            if module_path == "builtins":
                return module_path, None, None, "BUILTIN"
            return module_path or None, None, None, "STDLIB"
        if namespace not in {".", "local"}:
            return module_path or None, namespace, version, "THIRD_PARTY"
    return None, None, None, "UNKNOWN_EXTERNAL"


def parse_index(
    index: dict[str, Any], sources: dict[str, bytes]
) -> tuple[list[ScipOccurrence], list[Diagnostic]]:
    occurrences: list[ScipOccurrence] = []
    diagnostics: list[Diagnostic] = []
    metadata = index.get("metadata", {})
    index_encoding = metadata.get("text_document_encoding") if isinstance(metadata, dict) else None
    for document in index.get("documents", []):
        path = document.get("relativePath") or document.get("relative_path")
        if (
            not isinstance(path, str)
            or path not in sources
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SCIP_DOCUMENT_MISSING,
                    message="SCIP document does not map to an analyzed snapshot file.",
                    file_path=path if isinstance(path, str) else None,
                )
            )
            continue
        encoding = document.get(
            "positionEncoding",
            document.get("position_encoding", index_encoding if index_encoding is not None else 0),
        )
        content = sources[path]
        for raw in document.get("occurrences", []):
            symbol = raw.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                continue
            try:
                start_line, start_char, end_line, end_char = _range(raw)
                start_byte = position_to_byte(content, start_line, start_char, encoding)
                end_byte = position_to_byte(content, end_line, end_char, encoding)
                if end_byte < start_byte:
                    raise ScipIndexError("SCIP range ends before it starts")
            except (KeyError, ScipIndexError, UnicodeDecodeError) as exc:
                diagnostics.append(
                    Diagnostic(
                        code=(
                            DiagnosticCode.SCIP_POSITION_ENCODING_UNSPECIFIED
                            if encoding in (0, "UnspecifiedPositionEncoding", None)
                            else DiagnosticCode.RESOLVER_ERROR
                        ),
                        message=str(exc),
                        file_path=path,
                    )
                )
                continue
            start_prefix = content[:start_byte]
            end_prefix = content[:end_byte]
            occurrences.append(
                ScipOccurrence(
                    path=path,
                    document=str(document.get("uri", path)),
                    position_encoding=(
                        {
                            1: "UTF8CodeUnitOffsetFromLineStart",
                            2: "UTF16CodeUnitOffsetFromLineStart",
                            3: "UTF32CodeUnitOffsetFromLineStart",
                        }.get(encoding, str(encoding))
                    ),
                    span=SourceSpan(
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_prefix.count(b"\n") + 1,
                        start_column=len(start_prefix.rsplit(b"\n", 1)[-1]),
                        end_line=end_prefix.count(b"\n") + 1,
                        end_column=len(end_prefix.rsplit(b"\n", 1)[-1]),
                    ),
                    symbol=symbol,
                    symbol_roles=int(raw.get("symbolRoles", raw.get("symbol_roles", 0))),
                )
            )
    return occurrences, diagnostics


def join_bindings(
    analysis: SnapshotAnalysis,
    sources: dict[str, bytes],
    index: dict[str, Any],
    *,
    environment_id: str,
    extractor_version: str,
) -> tuple[list[SemanticBinding], list[ExternalSymbol], list[ProvenanceRecord], list[Diagnostic]]:
    occurrences, diagnostics = parse_index(index, sources)

    # SCIP local symbols are scoped to a document. Their numeric names can be
    # reused in unrelated files, so joining them repository-wide invents edges.
    def definition_key(occurrence: ScipOccurrence) -> tuple[str | None, str]:
        return (
            occurrence.path if occurrence.symbol.startswith("local ") else None,
            occurrence.symbol,
        )

    definitions: dict[tuple[str | None, str], list[SourceEntity]] = {}
    for occurrence in occurrences:
        if not occurrence.symbol_roles & 0x1:
            continue
        raw = sources[occurrence.path][occurrence.span.start_byte : occurrence.span.end_byte]
        candidates = [
            entity
            for entity in analysis.entities
            if entity.file_path == occurrence.path
            and entity.kind in {"Class", "Function", "Method"}
            and entity.span.start_byte <= occurrence.span.start_byte
            and occurrence.span.end_byte <= entity.span.end_byte
            and raw.decode("utf-8", "replace") == entity.name
        ]
        if candidates:
            candidates.sort(key=lambda item: item.span.end_byte - item.span.start_byte)
            definitions.setdefault(definition_key(occurrence), []).append(candidates[0])
    external_symbols_raw = index.get("externalSymbols", index.get("external_symbols", []))
    external_names = {
        item["symbol"]
        for item in external_symbols_raw
        if isinstance(item, dict) and isinstance(item.get("symbol"), str)
    }
    manifest = {item.path: item for item in analysis.manifest}
    bindings: list[SemanticBinding] = []
    external: dict[str, ExternalSymbol] = {}
    provenance: list[ProvenanceRecord] = []

    for site in analysis.sites:
        if site.kind == SiteKind.IMPORT_REFERENCE:
            continue
        call_reference_span = (
            _callable_reference_span(site) if site.kind == SiteKind.CALL_CALLEE else None
        )
        if site.kind == SiteKind.CALL_CALLEE and call_reference_span is None:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.UNRESOLVED_NAME,
                    message=(
                        "Callable expression has no unique syntax-grounded SCIP reference; "
                        "exact binding was withheld."
                    ),
                    file_path=site.file_path,
                    span=site.span,
                )
            )
            continue
        if site.kind == SiteKind.CALL_CALLEE:
            assert call_reference_span is not None
            reference_span, super_span = call_reference_span
            site_occurrences = [
                occurrence
                for occurrence in occurrences
                if occurrence.path == site.file_path
                and not occurrence.symbol_roles & 0x1
                and (occurrence.span.start_byte, occurrence.span.end_byte) == reference_span
            ]
            if super_span is not None:
                super_occurrences = [
                    occurrence
                    for occurrence in occurrences
                    if occurrence.path == site.file_path
                    and not occurrence.symbol_roles & 0x1
                    and (occurrence.span.start_byte, occurrence.span.end_byte) == super_span
                    and _external_metadata(occurrence.symbol)[-1] == "BUILTIN"
                ]
                if not super_occurrences:
                    diagnostics.append(
                        Diagnostic(
                            code=DiagnosticCode.UNRESOLVED_NAME,
                            message=(
                                "super() receiver was not independently resolved as the "
                                "built-in; exact binding was withheld."
                            ),
                            file_path=site.file_path,
                            span=site.span,
                        )
                    )
                    continue
        else:
            site_occurrences = [
                occurrence
                for occurrence in occurrences
                if occurrence.path == site.file_path
                and not occurrence.symbol_roles & 0x1
                and occurrence.span.end_byte == site.span.end_byte
                and occurrence.span.start_byte >= site.span.start_byte
            ]
            if not site_occurrences:
                site_occurrences = [
                    occurrence
                    for occurrence in occurrences
                    if occurrence.path == site.file_path
                    and not occurrence.symbol_roles & 0x1
                    and occurrence.span.start_byte < site.span.end_byte
                    and site.span.start_byte < occurrence.span.end_byte
                ]
        if not site_occurrences:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.UNRESOLVED_NAME,
                    message="SCIP emitted no semantic occurrence for this syntax site.",
                    file_path=site.file_path,
                    span=site.span,
                )
            )
            continue
        if site.kind == SiteKind.CALL_CALLEE:
            best = site_occurrences
        else:
            site_occurrences.sort(
                key=lambda item: (item.span.end_byte - item.span.start_byte, item.symbol)
            )
            best_width = site_occurrences[0].span.end_byte - site_occurrences[0].span.start_byte
            best = [
                item
                for item in site_occurrences
                if item.span.end_byte - item.span.start_byte == best_width
            ]
        unique_symbols = sorted({item.symbol for item in best})
        if len(unique_symbols) != 1:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.RESOLVER_ERROR,
                    message="Multiple SCIP symbols map to the same source occurrence.",
                    file_path=site.file_path,
                    span=site.span,
                )
            )
            continue
        scip_symbol = unique_symbols[0]
        targets = definitions.get(definition_key(best[0]), [])
        target_id: str | None = None
        external_id: str | None = None
        if len({target.entity_id for target in targets}) == 1:
            target_id = targets[0].entity_id
        elif targets:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.RESOLVER_ERROR,
                    message=(
                        "One SCIP symbol maps to multiple source definitions; "
                        "exact binding withheld."
                    ),
                    file_path=site.file_path,
                    span=site.span,
                )
            )
            continue
        else:
            if scip_symbol.startswith("local "):
                diagnostics.append(
                    Diagnostic(
                        code=DiagnosticCode.UNRESOLVED_NAME,
                        message=(
                            "Document-local SCIP reference has no mapped definition in "
                            "this file; exact binding was withheld."
                        ),
                        file_path=site.file_path,
                        span=site.span,
                    )
                )
                continue
            module_name, distribution_name, distribution_version, external_kind = (
                _external_metadata(scip_symbol)
            )
            if scip_symbol in external_names or external_kind in {"BUILTIN", "STDLIB"}:
                external_id = stable_id(environment_id, scip_symbol, prefix="ext_")
                external.setdefault(
                    scip_symbol,
                    ExternalSymbol(
                        external_symbol_id=external_id,
                        semantic_symbol=scip_symbol,
                        environment_id=environment_id,
                        module_name=module_name,
                        distribution_name=distribution_name,
                        distribution_version=distribution_version,
                        external_kind=external_kind,
                    ),
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        code=DiagnosticCode.UNRESOLVED_NAME,
                        message=(
                            "SCIP occurrence has no mapped in-repository definition "
                            "or external symbol record."
                        ),
                        file_path=site.file_path,
                        span=site.span,
                    )
                )
                continue

        target = target_id or external_id
        assert target is not None
        edge_id = stable_id(
            "static", "BINDS_TO", site.site_id, target, best[0].span.start_byte, prefix="edge_"
        )
        bindings.append(
            SemanticBinding(
                binding_id=stable_id(edge_id, prefix="bind_"),
                edge_id=edge_id,
                site_id=site.site_id,
                target_entity_id=target_id,
                external_symbol_id=external_id,
                scip_symbol=scip_symbol,
                occurrence_start_byte=best[0].span.start_byte,
            )
        )
        entry = manifest[site.file_path]
        provenance.append(
            ProvenanceRecord(
                provenance_id=stable_id(edge_id, prefix="prov_"),
                edge_id=edge_id,
                snapshot_id=analysis.snapshot_id,
                file_path=site.file_path,
                file_content_hash=entry.content_hash,
                span=best[0].span,
                syntax_node_type="scip_occurrence",
                extractor_name="scip-python",
                extractor_version=extractor_version,
                rule_id="scip.binding.v1",
                scip_document=best[0].document,
                scip_symbol=scip_symbol,
                scip_position_encoding=best[0].position_encoding,
                replay_scope=ReplayScope.SNAPSHOT_SEMANTIC,
            )
        )
    bindings.sort(key=lambda item: (item.site_id, item.edge_id))
    return bindings, list(external.values()), provenance, diagnostics


def run_scip_index(
    analysis: SnapshotAnalysis,
    settings: Any,
    *,
    timeout_seconds: int = 900,
    cancellation_event: threading.Event | None = None,
) -> tuple[dict[str, Any] | None, str | None, list[Diagnostic]]:
    """Run configured SCIP tools only over the already materialized snapshot workspace."""
    scip_python = _binary_path(settings, "scip_python_bin", "scip-python")
    scip_cli = _binary_path(settings, "scip_cli_bin", "scip")
    if not scip_python or not scip_cli:
        missing = [
            name for name, value in (("scip-python", scip_python), ("scip", scip_cli)) if not value
        ]
        return (
            None,
            None,
            [
                Diagnostic(
                    code=DiagnosticCode.SCIP_UNAVAILABLE,
                    message=(
                        f"Semantic indexing requires configured executables: {', '.join(missing)}."
                    ),
                )
            ],
        )

    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    workspace = cache_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in analysis.manifest:
        if not entry.analyzed:
            continue
        if Path(entry.path).is_absolute() or ".." in Path(entry.path).parts:
            raise ScipIndexError("Manifest path escaped the snapshot workspace")
        blob = cache_root / "blobs" / entry.blob_hash
        target = workspace / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(blob, target)

    index_path = cache_root / "index.scip"
    project_name = Path(analysis.repository.root).name or "local-project"
    command = [
        scip_python,
        "index",
        ".",
        "--project-name",
        project_name,
        "--project-version",
        analysis.repository.head_commit or analysis.snapshot_id,
        "--output",
        str(index_path),
    ]

    def run_command(command_args: list[str], deadline: float) -> subprocess.CompletedProcess[str]:
        if cancellation_event is None:
            return subprocess.run(
                command_args,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=max(0.01, deadline - time.monotonic()),
                check=False,
            )
        if cancellation_event.is_set():
            raise ScipCancelled("SCIP indexing was cancelled")
        process = subprocess.Popen(
            command_args,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    return subprocess.CompletedProcess(
                        command_args, process.returncode, stdout, stderr
                    )
                except subprocess.TimeoutExpired:
                    if cancellation_event.is_set():
                        process.terminate()
                        try:
                            process.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate()
                        raise ScipCancelled("SCIP indexing was cancelled") from None
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        raise subprocess.TimeoutExpired(command_args, timeout_seconds) from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    try:
        indexed = run_command(command, time.monotonic() + timeout_seconds)
        if indexed.returncode != 0 or not index_path.is_file():
            return (
                None,
                None,
                [
                    Diagnostic(
                        code=DiagnosticCode.RESOLVER_ERROR,
                        message="scip-python indexing failed; semantic bindings were withheld.",
                    )
                ],
            )
        printed = run_command(
            [scip_cli, "print", "--json", str(index_path)], time.monotonic() + timeout_seconds
        )
        if printed.returncode != 0:
            return (
                None,
                None,
                [
                    Diagnostic(
                        code=DiagnosticCode.RESOLVER_ERROR,
                        message="SCIP JSON decoding failed; semantic bindings were withheld.",
                    )
                ],
            )
        return json.loads(printed.stdout), _tool_version(scip_python), []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return (
            None,
            None,
            [
                Diagnostic(
                    code=DiagnosticCode.RESOLVER_ERROR,
                    message="SCIP indexing did not complete; semantic bindings were withheld.",
                )
            ],
        )


def _tool_version(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        return (result.stdout or result.stderr).strip()[:120] or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def scip_toolchain(settings: Any) -> tuple[str | None, str | None, str, str]:
    scip_python = _binary_path(settings, "scip_python_bin", "scip-python")
    scip_cli = _binary_path(settings, "scip_cli_bin", "scip")
    return (
        scip_python,
        scip_cli,
        _tool_version(scip_python) if scip_python else "unavailable",
        _tool_version(scip_cli) if scip_cli else "unavailable",
    )

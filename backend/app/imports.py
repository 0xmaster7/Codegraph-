from __future__ import annotations

import ast
from pathlib import PurePosixPath

from .ids import stable_id
from .models import (
    Diagnostic,
    DiagnosticCode,
    ExternalSymbol,
    ProvenanceRecord,
    SiteKind,
    SnapshotAnalysis,
    SourceSpan,
    StructuralImport,
)


def _module_target(
    current_module: str, file_path: str, node: ast.Import | ast.ImportFrom
) -> str | None:
    if isinstance(node, ast.Import):
        return None
    if not node.level:
        return node.module
    if node.module is None:
        return None
    module_parts = current_module.split(".")
    package_parts = (
        module_parts
        if PurePosixPath(file_path).name in {"__init__.py", "__init__.pyi"}
        else module_parts[:-1]
    )
    keep = len(package_parts) - node.level + 1
    if keep < 0:
        return None
    return ".".join([*package_parts[:keep], *node.module.split(".")])


def _span(source: bytes, node: ast.AST) -> SourceSpan:
    lines = source.splitlines(keepends=True)
    start_line = int(getattr(node, "lineno", 1))
    end_line = int(getattr(node, "end_lineno", start_line))
    start_column = int(getattr(node, "col_offset", 0))
    end_column = int(getattr(node, "end_col_offset", start_column))
    start_byte = sum(len(line) for line in lines[: start_line - 1]) + start_column
    end_byte = sum(len(line) for line in lines[: end_line - 1]) + end_column
    return SourceSpan(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )


def build_import_relationships(
    analysis: SnapshotAnalysis,
    sources: dict[str, bytes],
    external_symbols: list[ExternalSymbol],
) -> tuple[list[StructuralImport], list[ProvenanceRecord], list[Diagnostic]]:
    """Resolve import syntax only to exact indexed local modules or SCIP externals."""
    modules = {item.file_path: item for item in analysis.entities if item.kind == "Module"}
    modules_by_name = {item.qualified_name: item for item in modules.values()}
    external_by_name = {
        item.module_name: item for item in external_symbols if item.module_name is not None
    }
    sites_by_file = [site for site in analysis.sites if site.kind == SiteKind.IMPORT_REFERENCE]
    manifest = {item.path: item for item in analysis.manifest}
    facts: list[StructuralImport] = []
    provenance: list[ProvenanceRecord] = []
    diagnostics: list[Diagnostic] = []

    for path, source in sorted(sources.items()):
        source_module = modules.get(path)
        if source_module is None:
            continue
        try:
            parsed = ast.parse(source, filename=path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(parsed):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Import):
                target_names = [alias.name for alias in node.names]
            else:
                base = _module_target(source_module.qualified_name, path, node)
                if base:
                    target_names = [base]
                elif node.module is None and node.level:
                    package_parts = source_module.qualified_name.split(".")
                    if PurePosixPath(path).name not in {"__init__.py", "__init__.pyi"}:
                        package_parts = package_parts[:-1]
                    keep = len(package_parts) - node.level + 1
                    package = package_parts[:keep] if keep >= 0 else []
                    target_names = [".".join([*package, alias.name]) for alias in node.names]
                else:
                    target_names = []
            node_span = _span(source, node)
            site = next(
                (
                    item
                    for item in sites_by_file
                    if item.file_path == path
                    and item.span.start_byte <= node_span.start_byte
                    and item.span.end_byte >= node_span.end_byte
                ),
                None,
            )
            for imported in sorted(set(target_names)):
                if site is None:
                    continue
                target_module = modules_by_name.get(imported)
                target_external = external_by_name.get(imported)
                if target_module is None and target_external is None:
                    diagnostics.append(
                        Diagnostic(
                            code=DiagnosticCode.UNRESOLVED_NAME,
                            message=(
                                f"Import target {imported!r} has no exact local module or "
                                "SCIP external-symbol identity; no IMPORTS edge was asserted."
                            ),
                            file_path=path,
                            span=node_span,
                        )
                    )
                    continue
                if target_module is not None:
                    target_id = target_module.entity_id
                elif target_external is not None:
                    target_id = target_external.external_symbol_id
                else:
                    continue
                resolver = (
                    "repository-module-index"
                    if target_module is not None
                    else "scip-python-external"
                )
                edge_id = stable_id(
                    "static",
                    "IMPORTS",
                    source_module.entity_id,
                    target_id,
                    node_span.start_byte,
                    prefix="edge_",
                )
                fact = StructuralImport(
                    edge_id=edge_id,
                    site_id=site.site_id,
                    source_module_id=source_module.entity_id,
                    target_module_id=target_module.entity_id if target_module else None,
                    target_external_symbol_id=(
                        target_external.external_symbol_id if target_external else None
                    ),
                    imported_module=imported,
                    resolver=resolver,
                    span=node_span,
                )
                facts.append(fact)
                entry = manifest[path]
                provenance.append(
                    ProvenanceRecord(
                        provenance_id=stable_id(edge_id, prefix="prov_"),
                        edge_id=edge_id,
                        snapshot_id=analysis.snapshot_id,
                        file_path=path,
                        file_content_hash=entry.content_hash,
                        span=node_span,
                        syntax_node_type=type(node).__name__,
                        extractor_name=(
                            "scip-python" if target_external else "python-import-syntax"
                        ),
                        extractor_version=(
                            analysis.analysis_environment.scip_python_version
                            if target_external and analysis.analysis_environment
                            else "1"
                        ),
                        rule_id=f"imports.{resolver}.v1",
                        scip_document=path if target_external else None,
                        scip_symbol=(target_external.semantic_symbol if target_external else None),
                    )
                )
    facts.sort(key=lambda item: (item.source_module_id, item.span.start_byte, item.edge_id))
    return facts, provenance, diagnostics

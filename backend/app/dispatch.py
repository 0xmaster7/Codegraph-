from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from .ids import stable_id
from .models import (
    DerivedEdge,
    Diagnostic,
    DiagnosticCode,
    DispatchState,
    ProvenanceRecord,
    ReplayScope,
    SiteDispatch,
    SiteKind,
    SnapshotAnalysis,
    SourceEntity,
    SourceSite,
)


def _definition_edge(entity: SourceEntity) -> str:
    parent_id = entity.parent_id
    relation = "DEFINES_METHOD" if entity.kind == "Method" else "DEFINES"
    return stable_id(
        "static", relation, parent_id, entity.entity_id, entity.span.start_byte, prefix="edge_"
    )


def _node_bounds(node: ast.AST, source: bytes) -> tuple[int, int]:
    start_line = getattr(node, "lineno", 1) - 1
    end_line = getattr(node, "end_lineno", start_line + 1) - 1
    start_col = getattr(node, "col_offset", 0)
    end_col = getattr(node, "end_col_offset", start_col)
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[:start_line])) + start_col
    end = sum(map(len, lines[:end_line])) + end_col
    return start, end


def _classify_dynamic_hierarchy(
    entities: list[SourceEntity], sites: list[SourceSite], sources: dict[str, bytes]
) -> set[str]:
    unknown: set[str] = set()
    by_file = {path: content for path, content in sources.items()}
    classes = [entity for entity in entities if entity.kind == "Class"]
    for class_entity in classes:
        content = by_file[class_entity.file_path]
        header = content[class_entity.span.start_byte : class_entity.span.end_byte].split(b":", 1)[
            0
        ]
        if re.search(rb"\bmetaclass\s*=", header):
            unknown.add(class_entity.entity_id)
        for site in sites:
            if (
                site.kind != SiteKind.BASE_CLASS_REFERENCE
                or site.file_path != class_entity.file_path
            ):
                continue
            if not (
                class_entity.span.start_byte <= site.span.start_byte < class_entity.span.end_byte
            ):
                continue
            if site.syntax_node_type not in {"identifier", "attribute"}:
                unknown.add(class_entity.entity_id)
    return unknown


def _c3_mro(
    class_id: str,
    direct_bases: dict[str, list[str]],
    unknown_classes: set[str],
    visiting: set[str] | None = None,
) -> list[str] | None:
    if class_id in unknown_classes:
        return None
    ancestors = set() if visiting is None else visiting
    if class_id in ancestors:
        return None
    visiting = ancestors | {class_id}
    bases = direct_bases.get(class_id, [])
    sequences: list[list[str]] = []
    for base in bases:
        base_mro = _c3_mro(base, direct_bases, unknown_classes, visiting)
        if base_mro is None:
            return None
        sequences.append(list(base_mro))
    sequences.append(list(bases))
    result = [class_id]
    while any(sequences):
        sequences = [sequence for sequence in sequences if sequence]
        candidate = next(
            (
                sequence[0]
                for sequence in sequences
                if all(sequence[0] not in other[1:] for other in sequences)
            ),
            None,
        )
        if candidate is None:
            return None
        result.append(candidate)
        for sequence in sequences:
            if sequence and sequence[0] == candidate:
                sequence.pop(0)
    return result


def _annotation_receiver_class(
    site: SourceSite,
    content: bytes,
    classes_by_file_name: dict[tuple[str, str], SourceEntity],
    methods_by_id: dict[str, SourceEntity],
    entities: dict[str, SourceEntity],
) -> str | None:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return None
    matching_call: ast.Call | None = None
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and _node_bounds(node.func, content) == (
            site.span.start_byte,
            site.span.end_byte,
        ):
            matching_call = node
            break
    if matching_call is None or not isinstance(matching_call.func, ast.Attribute):
        return None
    receiver = matching_call.func.value
    if not isinstance(receiver, ast.Name):
        return None
    receiver_name = receiver.id

    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _node_bounds(node, content)[0] <= site.span.start_byte <= _node_bounds(node, content)[1]
    ]
    functions.sort(key=lambda node: _node_bounds(node, content)[1] - _node_bounds(node, content)[0])
    if not functions:
        return None
    function = functions[0]
    if receiver_name in {"self", "cls"}:
        method = next(
            (
                item
                for item in methods_by_id.values()
                if item.file_path == site.file_path
                and item.name == function.name
                and item.span.start_byte <= site.span.start_byte <= item.span.end_byte
            ),
            None,
        )
        owner = entities.get(method.parent_id or "") if method else None
        return owner.entity_id if owner and owner.kind == "Class" else None
    args: Iterable[ast.arg] = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    argument = next((item for item in args if item.arg == receiver_name), None)
    annotation = argument.annotation if argument is not None else None
    if annotation is None:
        for node in ast.walk(function):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == receiver_name
                and isinstance(node.annotation, ast.Name)
            ):
                annotation = node.annotation
                break
    if isinstance(annotation, ast.Name):
        target = classes_by_file_name.get((site.file_path, annotation.id))
        return target.entity_id if target else None
    return None


def derive_dispatch(
    analysis: SnapshotAnalysis,
    sources: dict[str, bytes],
) -> tuple[list[DerivedEdge], list[SiteDispatch], list[ProvenanceRecord], list[Diagnostic]]:
    entities = {entity.entity_id: entity for entity in analysis.entities}
    classes = {entity.entity_id: entity for entity in analysis.entities if entity.kind == "Class"}
    methods = {entity.entity_id: entity for entity in analysis.entities if entity.kind == "Method"}
    class_names = {entity.entity_id: entity.qualified_name for entity in classes.values()}
    bindings = {binding.site_id: binding for binding in analysis.bindings}
    class_by_file_name = {(item.file_path, item.name): item for item in classes.values()}
    direct_bases: dict[str, list[str]] = {class_id: [] for class_id in classes}
    inherits: list[DerivedEdge] = []
    dispatch: list[SiteDispatch] = []
    provenance: list[ProvenanceRecord] = []
    diagnostics: list[Diagnostic] = []
    manifest = {entry.path: entry for entry in analysis.manifest}
    unknown_classes = _classify_dynamic_hierarchy(analysis.entities, analysis.sites, sources)
    external_by_id = {
        external.external_symbol_id: external for external in analysis.external_symbols
    }

    for site in analysis.sites:
        if site.kind != SiteKind.BASE_CLASS_REFERENCE:
            continue
        owner = min(
            (
                entity
                for entity in classes.values()
                if entity.file_path == site.file_path
                and entity.span.start_byte <= site.span.start_byte < entity.span.end_byte
            ),
            key=lambda item: item.span.end_byte - item.span.start_byte,
            default=None,
        )
        if owner is None:
            continue
        binding = bindings.get(site.site_id)
        if binding is None:
            unknown_classes.add(owner.entity_id)
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.UNRESOLVED_NAME,
                    message="Base class could not be resolved; derived hierarchy is unknown.",
                    file_path=site.file_path,
                    span=site.span,
                )
            )
            continue
        target_id = binding.target_entity_id or binding.external_symbol_id
        if target_id is None:
            unknown_classes.add(owner.entity_id)
            continue
        if binding.target_entity_id in classes:
            direct_bases[owner.entity_id].append(binding.target_entity_id)
        elif binding.external_symbol_id:
            external = external_by_id.get(binding.external_symbol_id)
            if not (
                external
                and external.external_kind == "BUILTIN"
                and external.module_name == "builtins"
                and site.expression == "object"
            ):
                unknown_classes.add(owner.entity_id)
        edge_id = stable_id(
            "static", "INHERITS", owner.entity_id, target_id, site.span.start_byte, prefix="edge_"
        )
        edge = DerivedEdge(
            edge_id=edge_id,
            relation="INHERITS",
            source_id=owner.entity_id,
            target_id=target_id,
            rule_id="inheritance.scip-base.v1",
            premise_edge_ids=[binding.edge_id],
        )
        inherits.append(edge)
        entry = manifest[site.file_path]
        provenance.append(
            ProvenanceRecord(
                provenance_id=stable_id(edge_id, prefix="prov_"),
                edge_id=edge_id,
                snapshot_id=analysis.snapshot_id,
                file_path=site.file_path,
                file_content_hash=entry.content_hash,
                span=site.span,
                syntax_node_type=site.syntax_node_type,
                extractor_name="static-dispatch",
                extractor_version="1",
                rule_id=edge.rule_id,
                premise_edge_ids=edge.premise_edge_ids,
                replay_scope=ReplayScope.RULE_PREMISES,
            )
        )

    mros = {class_id: _c3_mro(class_id, direct_bases, unknown_classes) for class_id in classes}
    override_edges: list[DerivedEdge] = []
    methods_by_owner: dict[str, dict[str, SourceEntity]] = {}
    for method in methods.values():
        if method.parent_id in classes:
            methods_by_owner.setdefault(method.parent_id, {})[method.name] = method
    for class_id, method_map in methods_by_owner.items():
        mro = mros[class_id]
        if mro is None:
            continue
        for method in method_map.values():
            for ancestor_id in mro[1:]:
                overridden = methods_by_owner.get(ancestor_id, {}).get(method.name)
                if overridden is None:
                    continue
                premises = [
                    edge.edge_id
                    for edge in inherits
                    if edge.source_id in mro and edge.target_id in mro
                ]
                premises.extend([_definition_edge(method), _definition_edge(overridden)])
                edge_id = stable_id(
                    "static", "OVERRIDES", method.entity_id, overridden.entity_id, 0, prefix="edge_"
                )
                edge = DerivedEdge(
                    edge_id=edge_id,
                    relation="OVERRIDES",
                    source_id=method.entity_id,
                    target_id=overridden.entity_id,
                    rule_id="override.c3-name.v1",
                    premise_edge_ids=sorted(set(premises)),
                )
                override_edges.append(edge)
                entry = manifest[method.file_path]
                provenance.append(
                    ProvenanceRecord(
                        provenance_id=stable_id(edge_id, prefix="prov_"),
                        edge_id=edge_id,
                        snapshot_id=analysis.snapshot_id,
                        file_path=method.file_path,
                        file_content_hash=entry.content_hash,
                        span=method.span,
                        syntax_node_type="method_definition",
                        extractor_name="static-dispatch",
                        extractor_version="1",
                        rule_id=edge.rule_id,
                        premise_edge_ids=edge.premise_edge_ids,
                        replay_scope=ReplayScope.RULE_PREMISES,
                    )
                )
                break

    for site in analysis.sites:
        if site.kind != SiteKind.CALL_CALLEE:
            continue
        binding = bindings.get(site.site_id)
        target = entities.get(binding.target_entity_id or "") if binding else None
        if target and target.kind == "Method":
            assert binding is not None
            content = sources.get(site.file_path, b"")
            receiver_class = _annotation_receiver_class(
                site, content, class_by_file_name, methods, entities
            )
            if receiver_class is None:
                dispatch.append(SiteDispatch(site_id=site.site_id, state=DispatchState.UNKNOWN))
                continue
            static_class = receiver_class
            mro = mros.get(static_class)
            if mro is None:
                dispatch.append(SiteDispatch(site_id=site.site_id, state=DispatchState.UNKNOWN))
                continue
            mro_names = [class_names[item] for item in mro]
            if mro and not direct_bases.get(mro[-1]):
                mro_names.append("object")
            if target.parent_id not in mro:
                dispatch.append(
                    SiteDispatch(
                        site_id=site.site_id,
                        state=DispatchState.UNKNOWN,
                        mro_entity_ids=mro,
                        mro_qualified_names=mro_names,
                    )
                )
                continue
            possible_runtime_classes = [
                class_id
                for class_id, candidate_mro in mros.items()
                if candidate_mro is not None and static_class in candidate_mro
            ]
            if any(class_id in unknown_classes for class_id in possible_runtime_classes):
                dispatch.append(
                    SiteDispatch(
                        site_id=site.site_id,
                        state=DispatchState.UNKNOWN,
                        mro_entity_ids=mro,
                        mro_qualified_names=mro_names,
                    )
                )
                continue
            candidates: set[str] = set()
            for runtime_class in possible_runtime_classes:
                runtime_mro = mros[runtime_class]
                if runtime_mro is None:
                    continue
                selected = next(
                    (
                        methods_by_owner.get(item, {}).get(target.name)
                        for item in runtime_mro
                        if target.name in methods_by_owner.get(item, {})
                    ),
                    None,
                )
                if selected:
                    candidates.add(selected.entity_id)
            if target.entity_id not in candidates:
                candidates.add(target.entity_id)
            candidate_ids = sorted(candidates)
            premise_ids = [binding.edge_id]
            premise_ids.extend(
                edge.edge_id
                for edge in inherits
                if edge.source_id in possible_runtime_classes
                or edge.target_id in possible_runtime_classes
            )
            premise_ids.extend(
                edge.edge_id
                for edge in override_edges
                if edge.source_id in candidates or edge.target_id in candidates
            )
            for candidate_id in candidate_ids:
                edge_id = stable_id(
                    "static", "MAY_DISPATCH_TO", site.site_id, candidate_id, 0, prefix="edge_"
                )
                edge = DerivedEdge(
                    edge_id=edge_id,
                    relation="MAY_DISPATCH_TO",
                    source_id=site.site_id,
                    target_id=candidate_id,
                    rule_id="dispatch.c3-candidates.v1",
                    premise_edge_ids=sorted(set(premise_ids)),
                )
                override_edges.append(edge)
                entry = manifest[site.file_path]
                provenance.append(
                    ProvenanceRecord(
                        provenance_id=stable_id(edge_id, prefix="prov_"),
                        edge_id=edge_id,
                        snapshot_id=analysis.snapshot_id,
                        file_path=site.file_path,
                        file_content_hash=entry.content_hash,
                        span=site.span,
                        syntax_node_type="dispatch_site",
                        extractor_name="static-dispatch",
                        extractor_version="1",
                        rule_id=edge.rule_id,
                        premise_edge_ids=edge.premise_edge_ids,
                        replay_scope=ReplayScope.RULE_PREMISES,
                    )
                )
            dispatch.append(
                SiteDispatch(
                    site_id=site.site_id,
                    state=(
                        DispatchState.MULTI_CANDIDATE
                        if len(candidate_ids) > 1
                        else DispatchState.SINGLE_CANDIDATE
                    ),
                    candidate_entity_ids=candidate_ids,
                    mro_entity_ids=mro,
                    mro_qualified_names=mro_names,
                )
            )
        elif site.syntax_node_type == "call" or binding is None:
            dispatch.append(SiteDispatch(site_id=site.site_id, state=DispatchState.UNKNOWN))
        else:
            target_id = binding.target_entity_id or binding.external_symbol_id
            if target_id is None:
                dispatch.append(SiteDispatch(site_id=site.site_id, state=DispatchState.UNKNOWN))
                continue
            edge_id = stable_id(
                "static", "MAY_DISPATCH_TO", site.site_id, target_id, 0, prefix="edge_"
            )
            edge = DerivedEdge(
                edge_id=edge_id,
                relation="MAY_DISPATCH_TO",
                source_id=site.site_id,
                target_id=target_id,
                rule_id="dispatch.exact-target.v1",
                premise_edge_ids=[binding.edge_id],
            )
            override_edges.append(edge)
            entry = manifest[site.file_path]
            provenance.append(
                ProvenanceRecord(
                    provenance_id=stable_id(edge_id, prefix="prov_"),
                    edge_id=edge_id,
                    snapshot_id=analysis.snapshot_id,
                    file_path=site.file_path,
                    file_content_hash=entry.content_hash,
                    span=site.span,
                    syntax_node_type=site.syntax_node_type,
                    extractor_name="static-dispatch",
                    extractor_version="1",
                    rule_id=edge.rule_id,
                    premise_edge_ids=edge.premise_edge_ids,
                    replay_scope=ReplayScope.RULE_PREMISES,
                )
            )
            dispatch.append(
                SiteDispatch(
                    site_id=site.site_id,
                    state=DispatchState.SINGLE_CANDIDATE,
                    candidate_entity_ids=[target_id] if target_id in entities else [],
                )
            )

    edges = [*inherits, *override_edges]
    return edges, dispatch, provenance, diagnostics

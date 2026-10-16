from __future__ import annotations

import hashlib
from collections import Counter

from .ids import stable_id
from .models import GitChangeStatus, SnapshotAnalysis


class SchemaValidationError(ValueError):
    """Raised when a structural fact cannot be safely published."""


def validate_analysis(analysis: SnapshotAnalysis) -> None:
    """Validate IDs, endpoints, snapshot scope, spans, and provenance coverage."""
    entity_by_id = {entity.entity_id: entity for entity in analysis.entities}
    site_by_id = {site.site_id: site for site in analysis.sites}
    external_by_id = {item.external_symbol_id: item for item in analysis.external_symbols}
    manifest_by_path = {entry.path: entry for entry in analysis.manifest}
    developer_by_id = {item.developer_id: item for item in analysis.developers}
    commit_by_id = {item.commit_id: item for item in analysis.commits}
    change_by_id = {item.change_id: item for item in analysis.changed_paths}
    requirement_by_id = {
        item.requirement_id: item for item in analysis.dependency_metadata.requirements
    }
    component_by_id = {item.component_id: item for item in analysis.dependency_metadata.components}
    distribution_by_id = {
        item.distribution_id: item for item in analysis.dependency_metadata.resolved_distributions
    }
    provenance_by_edge: dict[str, list] = {}
    errors: list[str] = []

    if len(entity_by_id) != len(analysis.entities):
        errors.append("duplicate source entity ID")
    if len(site_by_id) != len(analysis.sites):
        errors.append("duplicate site ID")
    if len(external_by_id) != len(analysis.external_symbols):
        errors.append("duplicate external symbol ID")
    for external in analysis.external_symbols:
        if external.semantic_symbol.startswith("local "):
            errors.append(
                f"external symbol {external.external_symbol_id} is document-local "
                "and cannot be published"
            )
    if len(manifest_by_path) != len(analysis.manifest):
        errors.append("duplicate manifest path")
    chunk_ids: set[str] = set()
    for chunk in analysis.retrieval_chunks:
        if chunk.chunk_id in chunk_ids:
            errors.append(f"duplicate retrieval chunk ID {chunk.chunk_id}")
        chunk_ids.add(chunk.chunk_id)
        entry = manifest_by_path.get(chunk.file_path)
        if chunk.snapshot_id != analysis.snapshot_id:
            errors.append(f"retrieval chunk {chunk.chunk_id} crosses snapshot scope")
        if entry is None or not entry.analyzed:
            errors.append(f"retrieval chunk {chunk.chunk_id} has no analyzed manifest source")
            continue
        if (
            chunk.source_start_byte < 0
            or chunk.source_end_byte > entry.size_bytes
            or chunk.source_start_byte >= chunk.source_end_byte
            or chunk.source_end_byte - chunk.source_start_byte != len(chunk.text.encode("utf-8"))
        ):
            errors.append(f"retrieval chunk {chunk.chunk_id} has an invalid source span")
        if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.content_hash:
            errors.append(f"retrieval chunk {chunk.chunk_id} has a stale content hash")
        if chunk.chunk_kind == "DOCUMENT":
            expected_source_id = stable_id(
                analysis.snapshot_id, "document-source", chunk.file_path, prefix="docsrc_"
            )
            if chunk.source_node_id != expected_source_id or not chunk.file_path.lower().endswith(
                (".md", ".markdown")
            ):
                errors.append(f"document chunk {chunk.chunk_id} has invalid document provenance")
        elif chunk.source_node_id not in entity_by_id:
            errors.append(f"code chunk {chunk.chunk_id} has no canonical source entity")
    if len(developer_by_id) != len(analysis.developers):
        errors.append("duplicate Git developer ID")
    if len(commit_by_id) != len(analysis.commits):
        errors.append("duplicate Git commit ID")
    if len(change_by_id) != len(analysis.changed_paths):
        errors.append("duplicate changed path ID")
    if len(requirement_by_id) != len(analysis.dependency_metadata.requirements):
        errors.append("duplicate dependency requirement ID")
    if len(component_by_id) != len(analysis.dependency_metadata.components):
        errors.append("duplicate project component ID")
    if len(distribution_by_id) != len(analysis.dependency_metadata.resolved_distributions):
        errors.append("duplicate resolved distribution ID")

    expected_edges: set[str] = set()

    def expect(relation: str, source_id: str, target_id: str, instance: int) -> str:
        edge_id = stable_id("static", relation, source_id, target_id, instance, prefix="edge_")
        expected_edges.add(edge_id)
        return edge_id

    for entity in analysis.entities:
        if entity.kind not in {"File", "Module", "Class", "Function", "Method"}:
            errors.append(f"entity {entity.entity_id} has unsupported kind {entity.kind}")
        entry = manifest_by_path.get(entity.file_path)
        if entry is None:
            errors.append(f"entity {entity.entity_id} points to missing file {entity.file_path}")
            continue
        if entity.span.end_byte > entry.size_bytes or entity.span.end_byte < entity.span.start_byte:
            errors.append(f"entity {entity.entity_id} has invalid source span")
        if entity.kind == "File":
            if entity.parent_id is not None:
                errors.append(f"file entity {entity.entity_id} cannot have a source parent")
            expect("CONTAINS", analysis.snapshot_id, entity.entity_id, 0)
        elif entity.kind == "Module":
            if (
                entity.parent_id not in entity_by_id
                or entity_by_id[entity.parent_id].kind != "File"
            ):
                errors.append(f"module {entity.entity_id} has no file parent")
            else:
                expect(
                    "DECLARES_MODULE", entity.parent_id, entity.entity_id, entity.span.start_byte
                )
        else:
            if entity.parent_id not in entity_by_id:
                errors.append(f"entity {entity.entity_id} has a missing parent")
            else:
                parent = entity_by_id[entity.parent_id]
                allowed_parents = {
                    "Class": {"Module", "Class", "Function", "Method"},
                    "Function": {"Module", "Class", "Function", "Method"},
                    "Method": {"Class"},
                }[entity.kind]
                if parent.kind not in allowed_parents:
                    errors.append(
                        f"{entity.kind} {entity.entity_id} has invalid {parent.kind} parent"
                    )
                relation = "DEFINES_METHOD" if entity.kind == "Method" else "DEFINES"
                expect(relation, entity.parent_id, entity.entity_id, entity.span.start_byte)

    for site in analysis.sites:
        entry = manifest_by_path.get(site.file_path)
        if entry is None:
            errors.append(f"site {site.site_id} points to missing file {site.file_path}")
            continue
        if site.context_id not in entity_by_id:
            errors.append(f"site {site.site_id} has a missing execution context")
        elif entity_by_id[site.context_id].kind not in {"Module", "Class", "Function", "Method"}:
            errors.append(f"site {site.site_id} has an invalid execution context type")
        if site.span.end_byte > entry.size_bytes or site.span.end_byte < site.span.start_byte:
            errors.append(f"site {site.site_id} has invalid source span")
        expect("OCCURS_IN", site.site_id, site.context_id, site.span.start_byte)

    binding_ids: set[str] = set()
    bound_sites: set[str] = set()
    for binding in analysis.bindings:
        if binding.binding_id in binding_ids:
            errors.append(f"duplicate semantic binding ID {binding.binding_id}")
        binding_ids.add(binding.binding_id)
        if binding.site_id in bound_sites:
            errors.append(f"source site {binding.site_id} has multiple exact bindings")
        bound_sites.add(binding.site_id)
        if binding.binding_state != "EXACT":
            errors.append(f"binding {binding.binding_id} has unsupported binding state")
        if binding.site_id not in site_by_id:
            errors.append(f"binding {binding.binding_id} has a missing source site")
        elif not (
            site_by_id[binding.site_id].span.start_byte
            <= binding.occurrence_start_byte
            < site_by_id[binding.site_id].span.end_byte
        ):
            errors.append(f"binding {binding.binding_id} does not point to its source occurrence")
        has_entity = binding.target_entity_id is not None
        has_external = binding.external_symbol_id is not None
        if has_entity == has_external:
            errors.append(f"binding {binding.binding_id} must have exactly one target")
        if binding.target_entity_id is not None and binding.target_entity_id not in entity_by_id:
            errors.append(f"binding {binding.binding_id} has a missing symbol target")
        if (
            binding.target_entity_id in entity_by_id
            and entity_by_id[binding.target_entity_id].kind == "File"
        ):
            errors.append(f"binding {binding.binding_id} targets a non-symbol file")
        if (
            binding.external_symbol_id is not None
            and binding.external_symbol_id not in external_by_id
        ):
            errors.append(f"binding {binding.binding_id} has a missing external target")
        if (
            binding.external_symbol_id in external_by_id
            and analysis.analysis_environment is not None
            and external_by_id[binding.external_symbol_id].environment_id
            != analysis.analysis_environment.environment_id
        ):
            errors.append(f"binding {binding.binding_id} crosses analysis environments")
        target_id = binding.target_entity_id or binding.external_symbol_id
        if target_id:
            expected_id = stable_id(
                "static",
                "BINDS_TO",
                binding.site_id,
                target_id,
                binding.occurrence_start_byte,
                prefix="edge_",
            )
            if binding.edge_id != expected_id:
                errors.append(f"binding {binding.binding_id} has a non-canonical edge ID")
            expected_edges.add(binding.edge_id)

    import_edge_ids: set[str] = set()
    for item in analysis.imports:
        if item.edge_id in import_edge_ids:
            errors.append(f"duplicate structural import edge ID {item.edge_id}")
        import_edge_ids.add(item.edge_id)
        if (
            item.site_id not in site_by_id
            or site_by_id[item.site_id].kind.value != "IMPORT_REFERENCE"
        ):
            errors.append(f"import edge {item.edge_id} has no import syntax site")
        source = entity_by_id.get(item.source_module_id)
        if source is None or source.kind != "Module":
            errors.append(f"import edge {item.edge_id} has no source module")
        has_module = item.target_module_id is not None
        has_external = item.target_external_symbol_id is not None
        if has_module == has_external:
            errors.append(f"import edge {item.edge_id} must have exactly one target")
        elif has_module:
            target = entity_by_id.get(item.target_module_id or "")
            if target is None or target.kind != "Module":
                errors.append(f"import edge {item.edge_id} has an invalid module target")
        elif item.target_external_symbol_id not in external_by_id:
            errors.append(f"import edge {item.edge_id} has a missing external target")
        target_id = item.target_module_id or item.target_external_symbol_id or ""
        expected_id = stable_id(
            "static",
            "IMPORTS",
            item.source_module_id,
            target_id,
            item.span.start_byte,
            prefix="edge_",
        )
        if item.edge_id != expected_id:
            errors.append(f"import edge {item.edge_id} has a non-canonical ID")
        expected_edges.add(item.edge_id)

    known_endpoints = set(entity_by_id) | set(site_by_id) | set(external_by_id)
    derived_ids: set[str] = set()
    for edge in analysis.derived_edges:
        if edge.edge_id in derived_ids:
            errors.append(f"duplicate derived edge ID {edge.edge_id}")
        derived_ids.add(edge.edge_id)
        if edge.relation not in {"INHERITS", "OVERRIDES", "MAY_DISPATCH_TO"}:
            errors.append(f"derived edge {edge.edge_id} has unsupported relation")
        if edge.source_id not in known_endpoints or edge.target_id not in known_endpoints:
            errors.append(f"derived edge {edge.edge_id} has a missing endpoint")
        if edge.relation == "INHERITS":
            if edge.source_id not in entity_by_id or entity_by_id[edge.source_id].kind != "Class":
                errors.append(f"inheritance edge {edge.edge_id} has a non-class source")
            if edge.target_id not in external_by_id and (
                edge.target_id not in entity_by_id or entity_by_id[edge.target_id].kind != "Class"
            ):
                errors.append(f"inheritance edge {edge.edge_id} has an invalid target")
        if edge.relation == "OVERRIDES" and (
            edge.source_id not in entity_by_id
            or entity_by_id[edge.source_id].kind != "Method"
            or edge.target_id not in entity_by_id
            or entity_by_id[edge.target_id].kind != "Method"
        ):
            errors.append(f"override edge {edge.edge_id} has a non-method endpoint")
        if edge.relation == "MAY_DISPATCH_TO":
            if (
                edge.source_id not in site_by_id
                or site_by_id[edge.source_id].kind.value != "CALL_CALLEE"
            ):
                errors.append(f"dispatch edge {edge.edge_id} has a non-call-site source")
            if edge.target_id not in external_by_id and (
                edge.target_id not in entity_by_id
                or entity_by_id[edge.target_id].kind not in {"Class", "Function", "Method"}
            ):
                errors.append(f"dispatch edge {edge.edge_id} has an invalid target")
        expected_edges.add(edge.edge_id)

    binding_by_site = {binding.site_id: binding for binding in analysis.bindings}
    for state in analysis.dispatch:
        if state.site_id not in site_by_id:
            errors.append(f"dispatch state points to missing site {state.site_id}")
        if any(candidate not in entity_by_id for candidate in state.candidate_entity_ids):
            errors.append(f"dispatch state {state.site_id} has a missing candidate")
        if any(
            entity_by_id[candidate].kind not in {"Class", "Function", "Method"}
            for candidate in state.candidate_entity_ids
            if candidate in entity_by_id
        ):
            errors.append(f"dispatch state {state.site_id} has an invalid candidate type")
        site_binding = binding_by_site.get(state.site_id)
        external_single_candidate = bool(
            site_binding and site_binding.external_symbol_id is not None
        )
        if (
            state.state.value == "SINGLE_CANDIDATE"
            and len(state.candidate_entity_ids) != 1
            and not external_single_candidate
        ):
            errors.append(f"dispatch state {state.site_id} requires one candidate")
        if state.state.value == "MULTI_CANDIDATE" and len(state.candidate_entity_ids) < 2:
            errors.append(f"dispatch state {state.site_id} requires multiple candidates")

    provenance_ids: set[str] = set()
    for record in analysis.provenance:
        if record.provenance_id in provenance_ids:
            errors.append(f"duplicate provenance ID {record.provenance_id}")
        provenance_ids.add(record.provenance_id)
        provenance_by_edge.setdefault(record.edge_id, []).append(record)
        if record.snapshot_id != analysis.snapshot_id:
            errors.append(f"provenance {record.provenance_id} crosses snapshot scope")
        entry = manifest_by_path.get(record.file_path)
        if entry is None:
            errors.append(f"provenance {record.provenance_id} points to missing file")
        elif record.file_content_hash != entry.content_hash:
            errors.append(f"provenance {record.provenance_id} has a stale file hash")
        if record.span.end_byte < record.span.start_byte:
            errors.append(f"provenance {record.provenance_id} has an invalid span")
        elif entry is not None and record.span.end_byte > entry.size_bytes:
            errors.append(f"provenance {record.provenance_id} exceeds source file size")

    counts = Counter({edge_id: len(records) for edge_id, records in provenance_by_edge.items()})
    for edge_id in expected_edges:
        if counts[edge_id] != 1:
            errors.append(f"edge {edge_id} has {counts[edge_id]} provenance records")
    unexpected = set(provenance_by_edge) - expected_edges
    for edge_id in sorted(unexpected):
        errors.append(f"provenance refers to unknown edge {edge_id}")
    for record in analysis.provenance:
        if not set(record.premise_edge_ids) <= expected_edges:
            errors.append(f"provenance {record.provenance_id} has a missing premise edge")

    for commit in analysis.commits:
        expected_id = f"{analysis.repository.repository_id}::commit::{commit.commit_sha}"
        if commit.commit_id != expected_id:
            errors.append(f"Git commit {commit.commit_id} has a non-canonical ID")
        expected_parents = [
            f"{analysis.repository.repository_id}::commit::{sha}"
            for sha in commit.parent_commit_shas
        ]
        if commit.parent_commit_ids != expected_parents:
            errors.append(f"Git commit {commit.commit_id} has non-canonical parent IDs")
        if commit.author_id not in developer_by_id or commit.committer_id not in developer_by_id:
            errors.append(f"Git commit {commit.commit_id} has a missing developer")
    for developer in analysis.developers:
        if developer.developer_id != stable_id(
            developer.name.casefold(), developer.email.casefold()
        ):
            errors.append(f"Git developer {developer.developer_id} has a non-canonical ID")
    for change in analysis.changed_paths:
        if change.commit_id not in commit_by_id:
            errors.append(f"changed path {change.change_id} has a missing commit")
        if change.parent_commit_sha is not None:
            parent_commit = commit_by_id.get(change.commit_id)
            if (
                parent_commit is None
                or change.parent_commit_sha not in parent_commit.parent_commit_shas
            ):
                errors.append(f"changed path {change.change_id} has an invalid parent commit")
        valid_paths = {
            GitChangeStatus.ADDED: change.old_path is None and change.new_path is not None,
            GitChangeStatus.MODIFIED: change.new_path is not None,
            GitChangeStatus.DELETED: change.old_path is not None and change.new_path is None,
            GitChangeStatus.RENAMED: change.old_path is not None and change.new_path is not None,
            GitChangeStatus.COPIED: change.old_path is not None and change.new_path is not None,
            GitChangeStatus.TYPE_CHANGED: change.new_path is not None,
        }[change.status]
        if not valid_paths:
            errors.append(f"changed path {change.change_id} has invalid paths for its status")
    for requirement in analysis.dependency_metadata.requirements:
        if requirement.component_id is not None and requirement.component_id not in component_by_id:
            errors.append(f"dependency {requirement.requirement_id} has a missing component")
    for component in analysis.dependency_metadata.components:
        if any(item not in requirement_by_id for item in component.dependency_requirement_ids):
            errors.append(f"project component {component.component_id} has a missing dependency")
        if any(item not in manifest_by_path for item in component.manifest_files):
            errors.append(f"project component {component.component_id} has a missing manifest")
        if any(item not in manifest_by_path for item in component.source_paths):
            errors.append(f"project component {component.component_id} has a missing source path")
    for distribution in analysis.dependency_metadata.resolved_distributions:
        if distribution.requirement_id not in requirement_by_id:
            errors.append(
                f"resolved distribution {distribution.distribution_id} has a missing declaration"
            )

    if errors:
        raise SchemaValidationError("; ".join(errors))

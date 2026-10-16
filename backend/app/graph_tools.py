from __future__ import annotations

import hashlib
import heapq
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .fusion import EvidenceItem, EvidenceType, normalize_graph_evidence
from .ids import stable_id
from .models import (
    FAIL_CLOSED_REPLAY_OUTCOMES,
    ReplayOutcome,
    ReplayResult,
    ReviewState,
    SnapshotAnalysis,
    SnapshotStatus,
    SourceEntity,
    SourceSite,
)
from .retrieval import get_source_span, resolve_symbol

MAX_TOOL_LIMIT = 500
MAX_PATH_CANDIDATES = 20_000


def _bounded_sorted(
    records: Iterable[dict[str, Any]],
    limit: int,
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], int]:
    """Select deterministic top-k records while keeping only O(limit) candidates."""
    candidate_count = 0

    def counted_records():
        nonlocal candidate_count
        for record in records:
            candidate_count += 1
            yield record

    return heapq.nsmallest(limit, counted_records(), key=key), candidate_count


class GraphToolRequest(BaseModel):
    """Validated, bounded input shared by direct deterministic graph tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    limit: int = Field(default=100, ge=1, le=MAX_TOOL_LIMIT)


class PathToolRequest(GraphToolRequest):
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"] = "STRICT_STATIC_BINDING"
    hop_limit: int = Field(default=3, ge=1, le=5)


class ImpactToolRequest(GraphToolRequest):
    target_id: str = Field(min_length=1)
    mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"] = "CONSERVATIVE_DISPATCH"
    hop_limit: int = Field(default=3, ge=1, le=5)


class GraphToolResult(BaseModel):
    """Canonical result envelope; passage evidence remains outside graph paths."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    tool: str
    results: tuple[dict[str, Any], ...] = ()
    result_limit: int
    candidate_count_before_limit: int
    candidate_count_is_lower_bound: bool = False
    truncated: bool = False
    ordering: tuple[str, ...] = ()
    mode: str | None = None
    hop_limit: int | None = None
    unresolved_barriers: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Arc:
    source_id: str
    target_id: str
    relation: str
    edge_ids: tuple[str, ...]
    site_id: str | None = None
    uncertain: bool = False

    def key(self) -> tuple[str, str, str, str, tuple[str, ...]]:
        return (
            self.source_id,
            self.target_id,
            self.relation,
            self.site_id or "",
            self.edge_ids,
        )


class DeterministicGraphTools:
    """Snapshot-scoped, deterministic graph and evidence queries.

    Runtime, source-byte, replay, lexical and vector data enter only through explicit
    adapters. They retain their own evidence modality and never add graph edges.
    """

    @classmethod
    def from_store(cls, analysis: SnapshotAnalysis, store: Any) -> DeterministicGraphTools:
        """Bind snapshot tools to FalkorDB and immutable local source/cache data."""
        if store.snapshot_status(analysis.snapshot_id) != "READY":
            raise KeyError("Only READY snapshots can be queried by deterministic graph tools")
        blob_root = (
            store.settings.snapshot_cache_dir.expanduser().resolve()
            / analysis.snapshot_id
            / "blobs"
        )
        blobs = {item.path: item.blob_hash for item in analysis.manifest if item.analyzed}

        def load_source(file_path: str) -> bytes | None:
            blob_hash = blobs.get(file_path)
            if blob_hash is None:
                return None
            from .securefs import read_repository_file

            opened = read_repository_file(blob_root, blob_hash)
            return opened[0] if opened is not None else None

        provider: Any = None

        def search_semantic(query: str, limit: int) -> Sequence[Mapping[str, Any]]:
            nonlocal provider
            if provider is None:
                from .embeddings import LocalMiniLMOnnxProvider

                provider = LocalMiniLMOnnxProvider(device="cpu")
            vectors, _ = provider.embed_texts([query])
            return store.query_chunk_embeddings(
                analysis.snapshot_id,
                provider.spec.identity,
                vectors[0],
                limit=limit,
            )

        def search_lexical(query: str, limit: int) -> Sequence[Mapping[str, Any]]:
            return store.query_chunk_fulltext(analysis.snapshot_id, query, limit=limit)

        def replay(edge_id: str) -> ReplayResult:
            from .provenance import replay_edge

            return replay_edge(analysis, edge_id, store.settings)

        return cls(
            analysis,
            source_loader=load_source,
            replay=replay,
            runtime_records=store.runtime_tool_records(analysis.snapshot_id),
            lexical_search=search_lexical,
            semantic_search=search_semantic,
            snapshot_guard=lambda snapshot_id: store.snapshot_status(snapshot_id) == "READY",
        )

    def __init__(
        self,
        analysis: SnapshotAnalysis,
        *,
        source_loader: Callable[[str], bytes | None] | None = None,
        replay: Callable[[str], ReplayResult] | None = None,
        runtime_records: Sequence[Mapping[str, Any]] = (),
        lexical_search: Callable[[str, int], Sequence[Mapping[str, Any]]] | None = None,
        semantic_search: Callable[[str, int], Sequence[Mapping[str, Any]]] | None = None,
        snapshot_guard: Callable[[str], bool] | None = None,
    ) -> None:
        if analysis.status != SnapshotStatus.READY:
            raise KeyError("Only READY snapshots can be queried by deterministic graph tools")
        self.analysis = analysis
        self.snapshot_id = analysis.snapshot_id
        self.source_loader = source_loader
        self.replay = replay
        self.runtime_records = tuple(runtime_records)
        self.lexical_search = lexical_search
        self.semantic_search = semantic_search
        self.snapshot_guard = snapshot_guard
        self.entities = {item.entity_id: item for item in analysis.entities}
        self.replay_outcomes_by_edge: dict[str, set[ReplayOutcome]] = defaultdict(set)
        for item in analysis.provenance:
            self.replay_outcomes_by_edge[item.edge_id].add(item.replay_state)
        self.fail_closed_edge_ids = {
            edge_id
            for edge_id, outcomes in self.replay_outcomes_by_edge.items()
            if outcomes.intersection(FAIL_CLOSED_REPLAY_OUTCOMES)
        }
        self.failed_closed_bindings_by_site: dict[str, list[Any]] = defaultdict(list)
        self.entities_by_file: dict[str, list[SourceEntity]] = defaultdict(list)
        for entity in analysis.entities:
            self.entities_by_file[entity.file_path].append(entity)
        self.sites = {item.site_id: item for item in analysis.sites}
        self.sites_by_context: dict[str, list[SourceSite]] = defaultdict(list)
        for site in analysis.sites:
            self.sites_by_context[site.context_id].append(site)
        self.bindings_by_site: dict[str, list[Any]] = defaultdict(list)
        self.binding_by_edge = {item.edge_id: item for item in analysis.bindings}
        self.bindings_by_target: dict[str, list[tuple[Any, SourceSite]]] = defaultdict(list)
        for binding in analysis.bindings:
            matched_site = self.sites.get(binding.site_id)
            if binding.edge_id in self.fail_closed_edge_ids:
                self.failed_closed_bindings_by_site[binding.site_id].append(binding)
                continue
            self.bindings_by_site[binding.site_id].append(binding)
            target_id = binding.target_entity_id or binding.external_symbol_id
            if matched_site is not None and target_id:
                self.bindings_by_target[target_id].append((binding, matched_site))
        self.imports_by_target: dict[str, list[Any]] = defaultdict(list)
        for import_edge in analysis.imports:
            if import_edge.edge_id in self.fail_closed_edge_ids:
                continue
            target_id = import_edge.target_module_id or import_edge.target_external_symbol_id
            if target_id:
                self.imports_by_target[target_id].append(import_edge)
        self.dispatch_by_site = {item.site_id: item for item in analysis.dispatch}
        self.derived_by_relation: dict[str, list[Any]] = defaultdict(list)
        self.derived_by_edge = {item.edge_id: item for item in analysis.derived_edges}
        for edge in analysis.derived_edges:
            if edge.edge_id in self.fail_closed_edge_ids:
                continue
            self.derived_by_relation[edge.relation].append(edge)
        self.provenance_by_edge: dict[str, list[Any]] = defaultdict(list)
        for item in analysis.provenance:
            self.provenance_by_edge[item.edge_id].append(item)
        self.chunks = {item.chunk_id: item for item in analysis.retrieval_chunks}
        self._arcs_by_mode: dict[str, list[_Arc]] = {}
        self._impact_reverse_by_mode: dict[str, dict[str, list[_Arc]]] = {}
        self.binding_edge_ids = {item.edge_id for item in analysis.bindings}
        self.derived_edge_ids = {item.edge_id for item in analysis.derived_edges}
        self.fail_closed_evidence_ids = {
            stable_id(
                self.snapshot_id,
                (
                    EvidenceType.STATIC_BINDING.value
                    if edge_id in self.binding_edge_ids
                    else EvidenceType.STATIC_DERIVATION.value
                    if edge_id in self.derived_edge_ids
                    else EvidenceType.STATIC_STRUCTURE.value
                ),
                edge_id,
                prefix="evidence_",
            )
            for edge_id in self.fail_closed_edge_ids
        }

    def _scope(self, snapshot_id: str) -> None:
        if snapshot_id != self.snapshot_id:
            raise KeyError("Tool request snapshot does not match the loaded analysis")
        if self.snapshot_guard is not None and not self.snapshot_guard(snapshot_id):
            raise KeyError("Only READY snapshots can be queried by deterministic graph tools")

    @staticmethod
    def _result(
        snapshot_id: str,
        tool: str,
        records: Iterable[Mapping[str, Any]],
        limit: int,
        *,
        ordering: Sequence[str],
        mode: str | None = None,
        hop_limit: int | None = None,
        barriers: Iterable[Mapping[str, Any]] = (),
        diagnostics: Iterable[str] = (),
        candidate_count: int | None = None,
        candidate_count_is_lower_bound: bool = False,
    ) -> GraphToolResult:
        materialized = [dict(item) for item in records]
        count = len(materialized) if candidate_count is None else candidate_count
        return GraphToolResult(
            snapshot_id=snapshot_id,
            tool=tool,
            results=tuple(materialized[:limit]),
            result_limit=limit,
            candidate_count_before_limit=count,
            candidate_count_is_lower_bound=candidate_count_is_lower_bound,
            truncated=count > limit or len(materialized) > limit,
            ordering=tuple(ordering),
            mode=mode,
            hop_limit=hop_limit,
            unresolved_barriers=tuple(dict(item) for item in barriers),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _entity_record(entity: SourceEntity) -> dict[str, Any]:
        return {
            "id": entity.entity_id,
            "kind": entity.kind,
            "name": entity.name,
            "qualified_name": entity.qualified_name,
            "file_path": entity.file_path,
            "start_byte": entity.span.start_byte,
            "end_byte": entity.span.end_byte,
        }

    @staticmethod
    def _site_record(site: SourceSite) -> dict[str, Any]:
        return {
            "site_id": site.site_id,
            "kind": site.kind.value,
            "expression": site.expression,
            "file_path": site.file_path,
            "start_byte": site.span.start_byte,
            "end_byte": site.span.end_byte,
            "context_id": site.context_id,
        }

    def _edge_record(
        self, edge_id: str, relation: str, source_id: str, target_id: str
    ) -> dict[str, Any]:
        provenance = sorted(
            self.provenance_by_edge.get(edge_id, ()), key=lambda item: item.provenance_id
        )
        return {
            "edge_id": edge_id,
            "relation": relation,
            "source_id": source_id,
            "target_id": target_id,
            "provenance_ids": [item.provenance_id for item in provenance],
            "evidence": self._edge_evidence(edge_id, relation, source_id, target_id),
        }

    def _edge_evidence(
        self, edge_id: str, relation: str, source_id: str, target_id: str
    ) -> dict[str, Any]:
        if edge_id in self.binding_edge_ids:
            evidence_type = EvidenceType.STATIC_BINDING
        elif edge_id in self.derived_edge_ids:
            evidence_type = EvidenceType.STATIC_DERIVATION
        else:
            evidence_type = EvidenceType.STATIC_STRUCTURE
        provenance = sorted(
            self.provenance_by_edge.get(edge_id, ()), key=lambda item: item.provenance_id
        )
        pointer = ",".join(item.provenance_id for item in provenance) or edge_id
        replay_outcome = self._edge_replay_outcome(edge_id)
        return normalize_graph_evidence(
            evidence_type=evidence_type,
            snapshot_id=self.snapshot_id,
            source_record_id=edge_id,
            display_label=f"{source_id} {relation} {target_id}",
            provenance_pointer=pointer,
            review_state=(
                ReviewState.REPLAY_FAILED
                if replay_outcome not in {None, ReplayOutcome.PASS, ReplayOutcome.NOT_RUN}
                else ReviewState.NONE
            ),
            replay_outcome=replay_outcome,
        ).model_dump(mode="json")

    def _edge_replay_outcome(self, edge_id: str) -> ReplayOutcome | None:
        outcomes = self.replay_outcomes_by_edge.get(edge_id, set())
        if not outcomes:
            return None
        for outcome in (
            ReplayOutcome.EDGE_NOT_REPRODUCED,
            ReplayOutcome.SOURCE_HASH_MISMATCH,
            ReplayOutcome.ERROR,
            ReplayOutcome.ENVIRONMENT_MISMATCH,
            ReplayOutcome.EXTRACTOR_VERSION_MISMATCH,
            ReplayOutcome.PREMISE_MISSING,
        ):
            if outcome in outcomes:
                return outcome
        return ReplayOutcome.PASS if ReplayOutcome.PASS in outcomes else ReplayOutcome.NOT_RUN

    def _replay_integrity_diagnostics(self) -> tuple[str, ...]:
        if not self.fail_closed_edge_ids:
            return ()
        return (
            "Relationships with EDGE_NOT_REPRODUCED or SOURCE_HASH_MISMATCH were excluded from "
            "exact graph evidence; inspect their provenance records for the integrity diagnostic.",
        )

    def _git_evidence(self, source_record_id: str, display_label: str) -> dict[str, Any]:
        return normalize_graph_evidence(
            evidence_type=EvidenceType.GIT_HISTORY,
            snapshot_id=self.snapshot_id,
            source_record_id=source_record_id,
            display_label=display_label,
            provenance_pointer=f"{self.snapshot_id}:git:{source_record_id}",
        ).model_dump(mode="json")

    def _passage_evidence(self, chunk: Any, evidence_type: EvidenceType) -> dict[str, Any]:
        pointer = (
            f"{chunk.snapshot_id}:{chunk.file_path}:{chunk.source_start_byte}:"
            f"{chunk.source_end_byte}:{chunk.content_hash}"
        )
        return EvidenceItem(
            evidence_id=stable_id(
                self.snapshot_id, evidence_type.value, chunk.chunk_id, prefix="evidence_"
            ),
            type=evidence_type,
            snapshot_id=self.snapshot_id,
            source_record_id=chunk.chunk_id,
            display_label=f"{chunk.file_path}:{chunk.source_start_byte}-{chunk.source_end_byte}",
            provenance_pointer=pointer,
            chunk_id=chunk.chunk_id,
            chunk_kind=chunk.chunk_kind,
            source_node_id=chunk.source_node_id,
            file_path=chunk.file_path,
            content_hash=chunk.content_hash,
            text=chunk.text,
        ).model_dump(mode="json")

    def _arc_evidence(self, arc: _Arc) -> list[dict[str, Any]]:
        records = []
        for edge_id in arc.edge_ids:
            site = self.sites.get(arc.site_id or "")
            if site is not None and edge_id == stable_id(
                "static",
                "OCCURS_IN",
                site.site_id,
                site.context_id,
                site.span.start_byte,
                prefix="edge_",
            ):
                relation, source_id, target_id = "OCCURS_IN", site.site_id, site.context_id
            else:
                binding = self.binding_by_edge.get(edge_id)
                derived = self.derived_by_edge.get(edge_id)
                if binding is not None:
                    relation = "BINDS_TO"
                    source_id = binding.site_id
                    target_id = binding.target_entity_id or binding.external_symbol_id or ""
                elif derived is not None:
                    relation = derived.relation
                    source_id, target_id = derived.source_id, derived.target_id
                else:
                    relation, source_id, target_id = (
                        arc.relation,
                        arc.source_id,
                        arc.target_id,
                    )
            records.append(self._edge_evidence(edge_id, relation, source_id, target_id))
        return records

    def resolve_symbol(self, request: GraphToolRequest, query: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        resolution = resolve_symbol(self.analysis, query)
        records = [
            {
                **self._entity_record(item),
                "matched_by": resolution.matched_by,
                "resolution_status": resolution.status,
            }
            for item in resolution.results
        ]
        return self._result(
            request.snapshot_id,
            "resolve_symbol",
            records,
            request.limit,
            ordering=("qualified_name", "file_path", "start_byte", "id"),
            diagnostics=("Symbol was not found.",) if not records else (),
        )

    def get_source_span(
        self, request: GraphToolRequest, file_path: str, start_byte: int, end_byte: int
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if self.source_loader is None:
            return self._result(
                request.snapshot_id,
                "get_source_span",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "end_byte"),
                diagnostics=("Immutable snapshot source loader is unavailable.",),
            )
        try:
            content = self.source_loader(file_path)
        except (OSError, ValueError):
            content = None
        if content is None:
            return self._result(
                request.snapshot_id,
                "get_source_span",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "end_byte"),
                diagnostics=("Snapshot source file is unavailable.",),
            )
        manifest_entry = next(
            (item for item in self.analysis.manifest if item.path == file_path and item.analyzed),
            None,
        )
        if (
            manifest_entry is None
            or hashlib.sha256(content).hexdigest() != manifest_entry.content_hash
        ):
            return self._result(
                request.snapshot_id,
                "get_source_span",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "end_byte"),
                diagnostics=("Snapshot source hash does not match its immutable manifest.",),
            )
        try:
            record = get_source_span(self.analysis, file_path, start_byte, end_byte, content)
        except (StopIteration, ValueError):
            return self._result(
                request.snapshot_id,
                "get_source_span",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "end_byte"),
                diagnostics=("Requested source span is invalid for this snapshot.",),
            )
        return self._result(
            request.snapshot_id,
            "get_source_span",
            (record.model_dump(mode="json"),),
            request.limit,
            ordering=("file_path", "start_byte", "end_byte"),
        )

    def find_call_sites(
        self, request: GraphToolRequest, *, context_id: str | None = None, query: str | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        entity = self.entities.get(context_id or "")
        if context_id and entity is None:
            return self._result(
                request.snapshot_id,
                "find_call_sites",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "site_id"),
                diagnostics=("Context entity was not found in this snapshot.",),
            )
        needle = (query or "").casefold()
        candidate_sites = (
            self.sites_by_context.get(context_id, ()) if context_id else self.analysis.sites
        )

        def call_site_records():
            for site in candidate_sites:
                if site.kind.value != "CALL_CALLEE":
                    continue
                if needle and needle not in site.expression.casefold():
                    continue
                bindings = self.bindings_by_site.get(site.site_id, ())
                historical_bindings = [
                    item for item in self.analysis.bindings if item.site_id == site.site_id
                ]
                dispatch = self.dispatch_by_site.get(site.site_id)
                yield {
                    **self._site_record(site),
                    "binding_state": (
                        "EXACT"
                        if bindings
                        else "REPLAY_INTEGRITY_FAILURE"
                        if any(
                            item.edge_id in self.fail_closed_edge_ids
                            for item in historical_bindings
                        )
                        else "UNRESOLVED"
                    ),
                    "target_ids": sorted(
                        binding.target_entity_id or binding.external_symbol_id or ""
                        for binding in bindings
                    ),
                    "dispatch_state": dispatch.state.value if dispatch else "UNKNOWN",
                }

        records, candidate_count = _bounded_sorted(
            call_site_records(),
            request.limit,
            key=lambda item: (item["file_path"], item["start_byte"], item["site_id"]),
        )
        return self._result(
            request.snapshot_id,
            "find_call_sites",
            records,
            request.limit,
            ordering=("file_path", "start_byte", "site_id"),
            diagnostics=("No call sites matched the request.",) if candidate_count == 0 else (),
            candidate_count=candidate_count,
        )

    def find_callers(self, request: GraphToolRequest, target_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if target_id not in self.entities and not any(
            item.external_symbol_id == target_id for item in self.analysis.external_symbols
        ):
            return self._result(
                request.snapshot_id,
                "find_callers",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "site_id"),
                mode="STRICT_STATIC_BINDING",
                diagnostics=("Target ID was not found in this snapshot.",),
            )

        def caller_records():
            for binding, site in self.bindings_by_target.get(target_id, ()):
                if site.kind.value != "CALL_CALLEE" or binding.binding_state != "EXACT":
                    continue
                caller = self.entities.get(site.context_id)
                yield {
                    **self._site_record(site),
                    "caller": self._entity_record(caller) if caller else None,
                    "target_id": target_id,
                    "edge_id": binding.edge_id,
                    "evidence": self._edge_evidence(
                        binding.edge_id, "BINDS_TO", site.site_id, target_id
                    ),
                    "provenance_ids": [
                        item.provenance_id
                        for item in sorted(
                            self.provenance_by_edge.get(binding.edge_id, ()),
                            key=lambda item: item.provenance_id,
                        )
                    ],
                }

        records, candidate_count = _bounded_sorted(
            caller_records(),
            request.limit,
            key=lambda item: (item["file_path"], item["start_byte"], item["site_id"]),
        )
        return self._result(
            request.snapshot_id,
            "find_callers",
            records,
            request.limit,
            ordering=("file_path", "start_byte", "site_id"),
            mode="STRICT_STATIC_BINDING",
            candidate_count=candidate_count,
            diagnostics=(
                ("Exact caller bindings were excluded after replay integrity failure.",)
                if any(
                    (binding.target_entity_id or binding.external_symbol_id) == target_id
                    and binding.edge_id in self.fail_closed_edge_ids
                    for binding in self.analysis.bindings
                )
                else ()
            ),
        )

    def find_references(self, request: GraphToolRequest, target_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if target_id not in self.entities and not any(
            item.external_symbol_id == target_id for item in self.analysis.external_symbols
        ):
            return self._result(
                request.snapshot_id,
                "find_references",
                (),
                request.limit,
                ordering=("file_path", "start_byte", "site_id"),
                diagnostics=("Target ID was not found in this snapshot.",),
            )

        def reference_records():
            for binding, site in self.bindings_by_target.get(target_id, ()):
                yield {
                    **self._site_record(site),
                    "resolution": "EXACT",
                    "edge_id": binding.edge_id,
                    "evidence": self._edge_evidence(
                        binding.edge_id,
                        "BINDS_TO",
                        site.site_id,
                        binding.target_entity_id or binding.external_symbol_id or "",
                    ),
                    "provenance_ids": [
                        item.provenance_id
                        for item in sorted(
                            self.provenance_by_edge.get(binding.edge_id, ()),
                            key=lambda item: item.provenance_id,
                        )
                    ],
                }

        records, candidate_count = _bounded_sorted(
            reference_records(),
            request.limit,
            key=lambda item: (item["file_path"], item["start_byte"], item["site_id"]),
        )
        return self._result(
            request.snapshot_id,
            "find_references",
            records,
            request.limit,
            ordering=("file_path", "start_byte", "site_id"),
            mode="EXACT_SEMANTIC_BINDING",
            candidate_count=candidate_count,
            diagnostics=(
                ("Exact references were excluded after replay integrity failure.",)
                if any(
                    (binding.target_entity_id or binding.external_symbol_id) == target_id
                    and binding.edge_id in self.fail_closed_edge_ids
                    for binding in self.analysis.bindings
                )
                else ()
            ),
        )

    def find_callees(self, request: GraphToolRequest, site_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        site = self.sites.get(site_id)
        if site is None or site.kind.value != "CALL_CALLEE":
            return self._result(
                request.snapshot_id,
                "find_callees",
                (),
                request.limit,
                ordering=("qualified_name", "target_id"),
                diagnostics=("Call site was not found in this snapshot.",),
            )

        def exact_callees():
            for binding in self.bindings_by_site.get(site_id, ()):
                target_id = binding.target_entity_id or binding.external_symbol_id
                target = self.entities.get(target_id or "")
                external = next(
                    (
                        item
                        for item in self.analysis.external_symbols
                        if item.external_symbol_id == target_id
                    ),
                    None,
                )
                yield {
                    "site_id": site_id,
                    "context_id": site.context_id,
                    "target_id": target_id,
                    "target": self._entity_record(target) if target else None,
                    "external_symbol": external.model_dump(mode="json") if external else None,
                    "resolution": "EXACT",
                    "edge_id": binding.edge_id,
                    "evidence": self._edge_evidence(
                        binding.edge_id, "BINDS_TO", site_id, target_id or ""
                    ),
                }

        records, candidate_count = _bounded_sorted(
            exact_callees(),
            request.limit,
            key=lambda item: (
                (item["target"] or {}).get("qualified_name")
                or (item["external_symbol"] or {}).get("semantic_symbol", ""),
                item["target_id"] or "",
            ),
        )
        dispatch = self.dispatch_by_site.get(site_id)
        if dispatch and candidate_count == 0:
            dispatch_edges = {
                edge.target_id: edge
                for edge in self.derived_by_relation.get("MAY_DISPATCH_TO", ())
                if edge.source_id == site_id
            }

            def conservative_callees():
                for candidate in dispatch.candidate_entity_ids:
                    edge = dispatch_edges.get(candidate)
                    yield {
                        "site_id": site_id,
                        "context_id": site.context_id,
                        "target_id": candidate,
                        "target": self._entity_record(self.entities[candidate])
                        if candidate in self.entities
                        else None,
                        "resolution": "CONSERVATIVE_DISPATCH",
                        "edge_id": edge.edge_id if edge is not None else None,
                    }

            records, candidate_count = _bounded_sorted(
                conservative_callees(),
                request.limit,
                key=lambda item: (item.get("target_id") or "",),
            )
        unresolved = (
            () if candidate_count else ("Call site has no exact or supported dispatch target.",)
        )
        if self.failed_closed_bindings_by_site.get(site_id):
            unresolved = (
                *unresolved,
                "Exact call binding was excluded after replay integrity failure.",
            )
        return self._result(
            request.snapshot_id,
            "find_callees",
            records,
            request.limit,
            ordering=("target_id",),
            mode="EXACT_THEN_CONSERVATIVE_DISPATCH",
            diagnostics=unresolved,
            candidate_count=candidate_count,
        )

    def find_importers(self, request: GraphToolRequest, target_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        known = target_id in self.entities or any(
            item.external_symbol_id == target_id for item in self.analysis.external_symbols
        )
        if not known:
            return self._result(
                request.snapshot_id,
                "find_importers",
                (),
                request.limit,
                ordering=("source_module_id", "site_id", "edge_id"),
                diagnostics=("Target ID was not found in this snapshot.",),
            )
        sites = self.sites

        def importer_records():
            for edge in self.imports_by_target.get(target_id, ()):
                site = sites.get(edge.site_id)
                source = self.entities.get(edge.source_module_id)
                yield {
                    "source_module_id": edge.source_module_id,
                    "source": self._entity_record(source) if source else None,
                    "target_id": target_id,
                    "imported_module": edge.imported_module,
                    "site": self._site_record(site) if site else None,
                    **self._edge_record(edge.edge_id, "IMPORTS", edge.source_module_id, target_id),
                }

        records, candidate_count = _bounded_sorted(
            importer_records(),
            request.limit,
            key=lambda item: (item["source_module_id"], item["site"]["site_id"], item["edge_id"]),
        )
        return self._result(
            request.snapshot_id,
            "find_importers",
            records,
            request.limit,
            ordering=("source_module_id", "site_id", "edge_id"),
            candidate_count=candidate_count,
        )

    def find_inheritance(
        self, request: GraphToolRequest, entity_id: str | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        known = (
            entity_id is None
            or entity_id in self.entities
            or any(item.external_symbol_id == entity_id for item in self.analysis.external_symbols)
        )
        if not known:
            return self._result(
                request.snapshot_id,
                "find_inheritance",
                (),
                request.limit,
                ordering=("source_id", "target_id", "edge_id"),
                diagnostics=("Entity ID was not found in this snapshot.",),
            )
        edges = self.derived_by_relation.get("INHERITS", [])
        if entity_id:
            edges = [item for item in edges if entity_id in {item.source_id, item.target_id}]
        records = [
            {
                **item.model_dump(mode="json"),
                "source": self._entity_record(self.entities[item.source_id])
                if item.source_id in self.entities
                else None,
                "target": self._entity_record(self.entities[item.target_id])
                if item.target_id in self.entities
                else None,
                "provenance_ids": [
                    value.provenance_id
                    for value in sorted(
                        self.provenance_by_edge.get(item.edge_id, ()),
                        key=lambda value: value.provenance_id,
                    )
                ],
                "evidence": self._edge_evidence(
                    item.edge_id, item.relation, item.source_id, item.target_id
                ),
            }
            for item in edges
        ]
        records.sort(key=lambda item: (item["source_id"], item["target_id"], item["edge_id"]))
        return self._result(
            request.snapshot_id,
            "find_inheritance",
            records,
            request.limit,
            ordering=("source_id", "target_id", "edge_id"),
        )

    def find_overrides(
        self, request: GraphToolRequest, entity_id: str | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        known = entity_id is None or entity_id in self.entities
        if not known:
            return self._result(
                request.snapshot_id,
                "find_overrides",
                (),
                request.limit,
                ordering=("source_id", "target_id", "edge_id"),
                diagnostics=("Entity ID was not found in this snapshot.",),
            )
        edges = self.derived_by_relation.get("OVERRIDES", [])
        if entity_id:
            edges = [item for item in edges if entity_id in {item.source_id, item.target_id}]
        records = [
            {
                **item.model_dump(mode="json"),
                "source": self._entity_record(self.entities[item.source_id])
                if item.source_id in self.entities
                else None,
                "target": self._entity_record(self.entities[item.target_id])
                if item.target_id in self.entities
                else None,
                "provenance_ids": [
                    value.provenance_id
                    for value in sorted(
                        self.provenance_by_edge.get(item.edge_id, ()),
                        key=lambda value: value.provenance_id,
                    )
                ],
                "evidence": self._edge_evidence(
                    item.edge_id, item.relation, item.source_id, item.target_id
                ),
            }
            for item in edges
        ]
        records.sort(key=lambda item: (item["source_id"], item["target_id"], item["edge_id"]))
        return self._result(
            request.snapshot_id,
            "find_overrides",
            records,
            request.limit,
            ordering=("source_id", "target_id", "edge_id"),
        )

    def find_dispatch_candidates(self, request: GraphToolRequest, site_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        site = self.sites.get(site_id)
        if site is None or site.kind.value != "CALL_CALLEE":
            return self._result(
                request.snapshot_id,
                "find_dispatch_candidates",
                (),
                request.limit,
                ordering=("target_id", "edge_id"),
                diagnostics=("Call site was not found in this snapshot.",),
            )
        state = self.dispatch_by_site.get(site_id)
        edges = {
            item.target_id: item
            for item in self.derived_by_relation.get("MAY_DISPATCH_TO", ())
            if item.source_id == site_id
        }
        records = [
            {
                "site": self._site_record(site),
                "dispatch_state": state.state.value if state else "UNKNOWN",
                "target_id": target_id,
                "target": self._entity_record(self.entities[target_id])
                if target_id in self.entities
                else None,
                **self._edge_record(
                    edges[target_id].edge_id,
                    "MAY_DISPATCH_TO",
                    site_id,
                    target_id,
                ),
                "premise_edge_ids": list(edges[target_id].premise_edge_ids),
            }
            for target_id in (state.candidate_entity_ids if state else ())
            if target_id in edges
        ]
        records.sort(key=lambda item: (item["target_id"], item["edge_id"]))
        return self._result(
            request.snapshot_id,
            "find_dispatch_candidates",
            records,
            request.limit,
            ordering=("target_id", "edge_id"),
            mode="CONSERVATIVE_DISPATCH",
            diagnostics=(
                *(("Dispatch is unresolved; no candidates were inferred.",) if not records else ()),
                *(
                    ("Dispatch edges were excluded after replay integrity failure.",)
                    if any(
                        edge.source_id == site_id and edge.edge_id in self.fail_closed_edge_ids
                        for edge in self.analysis.derived_edges
                    )
                    else ()
                ),
            ),
        )

    def _arcs(self, mode: str) -> list[_Arc]:
        cached = self._arcs_by_mode.get(mode)
        if cached is not None:
            return cached
        arcs: list[_Arc] = []
        for entity in self.analysis.entities:
            if entity.parent_id and entity.parent_id in self.entities:
                relation = "DEFINES_METHOD" if entity.kind == "Method" else "DEFINES"
                edge_id = stable_id(
                    "static",
                    relation,
                    entity.parent_id,
                    entity.entity_id,
                    entity.span.start_byte,
                    prefix="edge_",
                )
                if edge_id in self.fail_closed_edge_ids:
                    continue
                arcs.append(
                    _Arc(
                        entity.parent_id,
                        entity.entity_id,
                        relation,
                        (edge_id,),
                    )
                )
        for site in self.analysis.sites:
            edge_id = stable_id(
                "static",
                "OCCURS_IN",
                site.site_id,
                site.context_id,
                site.span.start_byte,
                prefix="edge_",
            )
            # The path projection represents caller -> target, while retaining the
            # exact site and persisted source edges as its evidence.
            for binding in self.bindings_by_site.get(site.site_id, ()):
                target_id = binding.target_entity_id or binding.external_symbol_id
                if site.kind.value == "CALL_CALLEE" and target_id:
                    arcs.append(
                        _Arc(
                            site.context_id,
                            target_id,
                            "CALL_SITE_BINDING",
                            (edge_id, binding.edge_id),
                            site.site_id,
                        )
                    )
        for import_edge in self.analysis.imports:
            if import_edge.edge_id in self.fail_closed_edge_ids:
                continue
            arcs.append(
                _Arc(
                    import_edge.source_module_id,
                    import_edge.target_module_id or import_edge.target_external_symbol_id or "",
                    "IMPORTS",
                    (import_edge.edge_id,),
                    import_edge.site_id,
                )
            )
        for derived_edge in self.analysis.derived_edges:
            if derived_edge.edge_id in self.fail_closed_edge_ids:
                continue
            if derived_edge.relation in {"INHERITS", "OVERRIDES"}:
                arcs.append(
                    _Arc(
                        derived_edge.source_id,
                        derived_edge.target_id,
                        derived_edge.relation,
                        (derived_edge.edge_id,),
                    )
                )
            elif derived_edge.relation == "MAY_DISPATCH_TO" and mode == "CONSERVATIVE_DISPATCH":
                dispatch_site = self.sites.get(derived_edge.source_id)
                if dispatch_site:
                    occurs = stable_id(
                        "static",
                        "OCCURS_IN",
                        dispatch_site.site_id,
                        dispatch_site.context_id,
                        dispatch_site.span.start_byte,
                        prefix="edge_",
                    )
                    arcs.append(
                        _Arc(
                            dispatch_site.context_id,
                            derived_edge.target_id,
                            "CONSERVATIVE_DISPATCH",
                            (occurs, derived_edge.edge_id),
                            dispatch_site.site_id,
                            True,
                        )
                    )
        result = sorted((arc for arc in arcs if arc.target_id), key=lambda arc: arc.key())
        self._arcs_by_mode[mode] = result
        return result

    def _barriers(self, context_ids: Iterable[str]) -> list[dict[str, Any]]:
        contexts = set(context_ids)
        records = []
        for site in self.analysis.sites:
            if site.kind.value != "CALL_CALLEE" or site.context_id not in contexts:
                continue
            if self.bindings_by_site.get(site.site_id):
                continue
            failed_bindings = self.failed_closed_bindings_by_site.get(site.site_id, ())
            if failed_bindings:
                records.append(
                    {
                        **self._site_record(site),
                        "resolution": "REPLAY_INTEGRITY_FAILURE",
                        "replay_outcomes": sorted(
                            {
                                outcome.value
                                for binding in failed_bindings
                                for outcome in self.replay_outcomes_by_edge.get(binding.edge_id, ())
                            }
                        ),
                    }
                )
                continue
            dispatch = self.dispatch_by_site.get(site.site_id)
            if dispatch and dispatch.candidate_entity_ids:
                continue
            records.append(
                {
                    **self._site_record(site),
                    "resolution": "UNRESOLVED",
                    "dispatch_state": dispatch.state.value if dispatch else "UNKNOWN",
                }
            )
        records.sort(key=lambda item: (item["file_path"], item["start_byte"], item["site_id"]))
        return records

    def _enumerate_paths(
        self, source_id: str, target_id: str, mode: str, hop_limit: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        if source_id not in self.entities and not any(
            item.external_symbol_id == source_id for item in self.analysis.external_symbols
        ):
            return [], [], False
        if target_id not in self.entities and not any(
            item.external_symbol_id == target_id for item in self.analysis.external_symbols
        ):
            return [], [], False
        adjacency: dict[str, list[_Arc]] = defaultdict(list)
        for arc in self._arcs(mode):
            adjacency[arc.source_id].append(arc)
        queue: deque[tuple[str, tuple[str, ...], tuple[_Arc, ...]]] = deque(
            [(source_id, (source_id,), ())]
        )
        found: list[dict[str, Any]] = []
        contexts: set[str] = {source_id}
        truncated = False
        explored = 0
        while queue:
            current, nodes, arcs = queue.popleft()
            if len(arcs) >= hop_limit:
                continue
            for arc in adjacency.get(current, ()):
                if arc.target_id in nodes:
                    continue
                explored += 1
                if explored > MAX_PATH_CANDIDATES:
                    truncated = True
                    queue.clear()
                    break
                next_nodes = (*nodes, arc.target_id)
                next_arcs = (*arcs, arc)
                contexts.add(arc.target_id)
                if arc.target_id == target_id:
                    found.append(
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "node_ids": list(next_nodes),
                            "hop_count": len(next_arcs),
                            "uncertainty_count": sum(item.uncertain for item in next_arcs),
                            "mode": mode,
                            "steps": [
                                {
                                    "source_id": item.source_id,
                                    "target_id": item.target_id,
                                    "relation": item.relation,
                                    "site_id": item.site_id,
                                    "edge_ids": list(item.edge_ids),
                                    "provenance_ids": sorted(
                                        provenance.provenance_id
                                        for edge_id in item.edge_ids
                                        for provenance in self.provenance_by_edge.get(edge_id, ())
                                    ),
                                    "evidence": self._arc_evidence(item),
                                    "verified_graph_path": True,
                                }
                                for item in next_arcs
                            ],
                        }
                    )
                else:
                    queue.append((arc.target_id, next_nodes, next_arcs))
        found.sort(
            key=lambda item: (
                item["hop_count"],
                item["uncertainty_count"],
                tuple(item["node_ids"]),
                tuple(step["edge_ids"] for step in item["steps"]),
            )
        )
        return found, self._barriers(contexts), truncated

    def find_paths(self, request: PathToolRequest) -> GraphToolResult:
        self._scope(request.snapshot_id)
        paths, barriers, search_truncated = self._enumerate_paths(
            request.source_id, request.target_id, request.mode, request.hop_limit
        )
        known_ids = set(self.entities) | {
            item.external_symbol_id for item in self.analysis.external_symbols
        }
        diagnostics = list(self._replay_integrity_diagnostics())
        if request.source_id not in known_ids:
            diagnostics.append("Source ID was not found in this snapshot.")
        if request.target_id not in known_ids:
            diagnostics.append("Target ID was not found in this snapshot.")
        if search_truncated:
            diagnostics.append("Path exploration reached its deterministic candidate safety bound.")
        result = self._result(
            request.snapshot_id,
            "find_paths",
            paths,
            request.limit,
            ordering=("hop_count", "uncertainty_count", "canonical_path_key"),
            mode=request.mode,
            hop_limit=request.hop_limit,
            barriers=barriers,
            diagnostics=diagnostics,
            candidate_count=len(paths) + (1 if search_truncated else 0),
            candidate_count_is_lower_bound=search_truncated,
        )
        if search_truncated and not result.truncated:
            return result.model_copy(update={"truncated": True})
        return result

    def find_shortest_evidence_path(self, request: PathToolRequest) -> GraphToolResult:
        self._scope(request.snapshot_id)
        paths, barriers, search_truncated = self._enumerate_paths(
            request.source_id, request.target_id, request.mode, request.hop_limit
        )
        results: list[dict[str, Any]] = []
        if paths:
            shortest = paths[0]["hop_count"]
            results = [item for item in paths if item["hop_count"] == shortest]
        diagnostics = list(self._replay_integrity_diagnostics())
        known_ids = set(self.entities) | {
            item.external_symbol_id for item in self.analysis.external_symbols
        }
        if request.source_id not in known_ids:
            diagnostics.append("Source ID was not found in this snapshot.")
        if request.target_id not in known_ids:
            diagnostics.append("Target ID was not found in this snapshot.")
        if search_truncated:
            diagnostics.append("Path exploration reached its deterministic candidate safety bound.")
        return self._result(
            request.snapshot_id,
            "find_shortest_evidence_path",
            results,
            request.limit,
            ordering=("hop_count", "uncertainty_count", "canonical_path_key"),
            mode=request.mode,
            hop_limit=request.hop_limit,
            barriers=barriers,
            diagnostics=diagnostics,
            candidate_count=len(results) + (1 if search_truncated else 0),
            candidate_count_is_lower_bound=search_truncated,
        )

    def expand_neighborhood(
        self,
        request: GraphToolRequest,
        entity_id: str,
        *,
        hop_limit: int = 1,
        mode: Literal["STRICT_STATIC_BINDING", "CONSERVATIVE_DISPATCH"] = "STRICT_STATIC_BINDING",
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if not 1 <= hop_limit <= 5:
            raise ValueError("Neighborhood hop limit must be between 1 and 5")
        known = set(self.entities) | {
            item.external_symbol_id for item in self.analysis.external_symbols
        }
        if entity_id not in known:
            return self._result(
                request.snapshot_id,
                "expand_neighborhood",
                (),
                request.limit,
                ordering=("distance", "node_id", "relation"),
                mode=mode,
                hop_limit=hop_limit,
                diagnostics=("Entity ID was not found in this snapshot.",),
            )
        adjacency: dict[str, list[tuple[str, _Arc]]] = defaultdict(list)
        for arc in self._arcs(mode):
            adjacency[arc.source_id].append((arc.target_id, arc))
            adjacency[arc.target_id].append((arc.source_id, arc))
        visited = {entity_id}
        queue = deque([(entity_id, 0)])
        records = []
        explored = 0
        search_truncated = False
        while queue:
            current, distance = queue.popleft()
            if distance >= hop_limit:
                continue
            for neighbor, arc in sorted(
                adjacency[current], key=lambda item: (item[0], item[1].key())
            ):
                explored += 1
                if explored > MAX_PATH_CANDIDATES:
                    search_truncated = True
                    queue.clear()
                    break
                records.append(
                    {
                        "node_id": neighbor,
                        "distance": distance + 1,
                        "from_id": current,
                        "relation": arc.relation,
                        "edge_ids": list(arc.edge_ids),
                        "site_id": arc.site_id,
                        "uncertain": arc.uncertain,
                        "evidence": self._arc_evidence(arc),
                    }
                )
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, distance + 1))
        records.sort(
            key=lambda item: (item["distance"], item["node_id"], item["relation"], item["from_id"])
        )
        result = self._result(
            request.snapshot_id,
            "expand_neighborhood",
            records,
            request.limit,
            ordering=("distance", "node_id", "relation", "from_id"),
            mode=mode,
            hop_limit=hop_limit,
            diagnostics=(
                *self._replay_integrity_diagnostics(),
                *(
                    ("Neighborhood exploration reached its deterministic candidate safety bound.",)
                    if search_truncated
                    else ()
                ),
            ),
            candidate_count_is_lower_bound=search_truncated,
        )
        if search_truncated and not result.truncated:
            return result.model_copy(update={"truncated": True})
        return result

    def find_impact(self, request: ImpactToolRequest) -> GraphToolResult:
        """Find bounded transitive dependents with ordered, snapshot-local path evidence."""
        self._scope(request.snapshot_id)
        known = set(self.entities) | {
            item.external_symbol_id for item in self.analysis.external_symbols
        }
        if request.target_id not in known:
            return self._result(
                request.snapshot_id,
                "find_impact",
                (),
                request.limit,
                ordering=("distance", "node_id", "path_key"),
                mode=request.mode,
                hop_limit=request.hop_limit,
                diagnostics=("Impact target was not found in this snapshot.",),
            )

        # Impact follows dependencies toward their users: reverse each persisted
        # caller/import/override/dispatch arc while retaining the canonical edge
        # evidence in source-to-target order.
        eligible_relations = {
            "CALL_SITE_BINDING",
            "IMPORTS",
            "INHERITS",
            "OVERRIDES",
            "CONSERVATIVE_DISPATCH",
        }
        reverse = self._impact_reverse_by_mode.get(request.mode)
        if reverse is None:
            reverse = defaultdict(list)
            for arc in self._arcs(request.mode):
                if arc.relation in eligible_relations:
                    reverse[arc.target_id].append(arc)
            for arcs in reverse.values():
                arcs.sort(key=lambda item: item.key())
            self._impact_reverse_by_mode[request.mode] = reverse

        # Queue entries carry a simple path in reverse traversal order.
        queue: deque[tuple[str, tuple[str, ...], tuple[_Arc, ...]]] = deque(
            [(request.target_id, (request.target_id,), ())]
        )
        best_distance = {request.target_id: 0}
        selected: dict[str, dict[str, Any]] = {}
        explored = 0
        safety_truncated = False
        while queue:
            current, reverse_nodes, reverse_arcs = queue.popleft()
            distance = len(reverse_arcs)
            if distance >= request.hop_limit:
                continue
            for arc in reverse.get(current, ()):
                explored += 1
                if explored > MAX_PATH_CANDIDATES:
                    safety_truncated = True
                    queue.clear()
                    break
                dependent = arc.source_id
                if dependent in reverse_nodes:
                    continue
                arcs_to_target = (*reverse_arcs, arc)
                next_distance = distance + 1
                forward_nodes = tuple(reversed((*reverse_nodes, dependent)))
                # Reverse traversal discovers dependent -> ... -> target after
                # reversing the nodes and the matching arc sequence.
                forward_arcs = tuple(reversed(arcs_to_target))
                steps = [
                    {
                        "source_id": item.source_id,
                        "target_id": item.target_id,
                        "relation": item.relation,
                        "edge_ids": list(item.edge_ids),
                        "site_id": item.site_id,
                        "uncertain": item.uncertain,
                        "evidence": self._arc_evidence(item),
                    }
                    for item in forward_arcs
                ]
                key = (next_distance, sum(item.uncertain for item in forward_arcs), forward_nodes)
                previous = selected.get(dependent)
                if previous is None or key < previous["_key"]:
                    selected[dependent] = {
                        "node_id": dependent,
                        "distance": next_distance,
                        "path": list(forward_nodes),
                        "steps": steps,
                        "uncertainty_count": key[1],
                        "mode": request.mode,
                        "snapshot_id": request.snapshot_id,
                        "_key": key,
                    }
                if next_distance < request.hop_limit and next_distance <= best_distance.get(
                    dependent, request.hop_limit
                ):
                    best_distance[dependent] = next_distance
                    queue.append((dependent, (*reverse_nodes, dependent), arcs_to_target))

        ordered = sorted(selected.values(), key=lambda item: item["_key"])
        for record in ordered:
            record.pop("_key", None)
            # Surface unresolved/replay-failed call sites in the traversed
            # contexts as barriers. They are contextual diagnostics, not path
            # steps and never increase the path's graph reachability claim.
            record["unresolved_barriers"] = self._barriers(record["path"])
        result = self._result(
            request.snapshot_id,
            "find_impact",
            ordered,
            request.limit,
            ordering=("distance", "uncertainty_count", "path", "node_id"),
            mode=request.mode,
            hop_limit=request.hop_limit,
            diagnostics=(
                *self._replay_integrity_diagnostics(),
                *(
                    ("Impact traversal reached its deterministic candidate safety bound.",)
                    if safety_truncated
                    else ()
                ),
            ),
            candidate_count=len(ordered) + (1 if safety_truncated else 0),
            candidate_count_is_lower_bound=safety_truncated,
        )
        if safety_truncated and not result.truncated:
            return result.model_copy(update={"truncated": True})
        return result

    def get_edge_provenance(self, request: GraphToolRequest, edge_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        records = [
            item.model_dump(mode="json")
            for item in sorted(
                self.provenance_by_edge.get(edge_id, ()), key=lambda item: item.provenance_id
            )
        ]
        return self._result(
            request.snapshot_id,
            "get_edge_provenance",
            records,
            request.limit,
            ordering=("provenance_id",),
            diagnostics=("No provenance record exists for this edge in this snapshot.",)
            if not records
            else (),
        )

    def replay_evidence(self, request: GraphToolRequest, edge_id: str) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if self.replay is None:
            return self._result(
                request.snapshot_id,
                "replay_evidence",
                (),
                request.limit,
                ordering=("edge_id",),
                diagnostics=("Evidence replay adapter is unavailable.",),
            )
        if edge_id not in self.provenance_by_edge:
            return self._result(
                request.snapshot_id,
                "replay_evidence",
                (),
                request.limit,
                ordering=("edge_id",),
                diagnostics=("Evidence ID was not found in this snapshot.",),
            )
        result = self.replay(edge_id)
        return self._result(
            request.snapshot_id,
            "replay_evidence",
            (result.model_dump(mode="json"),),
            request.limit,
            ordering=("edge_id",),
        )

    def get_git_history(
        self, request: GraphToolRequest, *, limit: int | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        final_limit = min(limit or request.limit, request.limit, MAX_TOOL_LIMIT)
        records = [item.model_dump(mode="json") for item in self.analysis.commits]
        for record in records:
            record["evidence"] = self._git_evidence(
                str(record["commit_id"]), f"Commit {record['commit_sha']}"
            )
        records.sort(
            key=lambda item: (item["committed_at"], item["commit_sha"], item["commit_id"]),
            reverse=True,
        )
        return self._result(
            request.snapshot_id,
            "get_git_history",
            records,
            final_limit,
            ordering=("committed_at_desc", "commit_sha", "commit_id"),
            diagnostics=("No Git history is available in this snapshot.",) if not records else (),
        )

    def find_changed_paths(
        self, request: GraphToolRequest, *, commit_id: str | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        records = [item.model_dump(mode="json") for item in self.analysis.changed_paths]
        for record in records:
            record["evidence"] = self._git_evidence(
                str(record["change_id"]),
                f"Git path change {record['old_path'] or record['new_path']}",
            )
        if commit_id is not None:
            known = {item.commit_id for item in self.analysis.commits}
            if commit_id not in known:
                return self._result(
                    request.snapshot_id,
                    "find_changed_paths",
                    (),
                    request.limit,
                    ordering=("commit_id", "parent_index", "old_path", "new_path", "change_id"),
                    diagnostics=("Commit ID was not found in this snapshot history.",),
                )
            records = [item for item in records if item["commit_id"] == commit_id]
        records.sort(
            key=lambda item: (
                item["commit_id"],
                item["parent_index"],
                item["old_path"] or "",
                item["new_path"] or "",
                item["change_id"],
            )
        )
        return self._result(
            request.snapshot_id,
            "find_changed_paths",
            records,
            request.limit,
            ordering=("commit_id", "parent_index", "old_path", "new_path", "change_id"),
        )

    def find_changed_symbols(
        self, request: GraphToolRequest, *, commit_id: str | None = None
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        changes = self.analysis.changed_paths
        if commit_id is not None:
            known = {item.commit_id for item in self.analysis.commits}
            if commit_id not in known:
                return self._result(
                    request.snapshot_id,
                    "find_changed_symbols",
                    (),
                    request.limit,
                    ordering=("file_path", "start_byte", "symbol_id"),
                    diagnostics=("Commit ID was not found in this snapshot history.",),
                )
            changes = [item for item in changes if item.commit_id == commit_id]
        records: dict[str, dict[str, Any]] = {}
        for change in changes:
            for file_path in (change.old_path, change.new_path):
                if file_path is None:
                    continue
                for entity in self.entities_by_file.get(file_path, ()):
                    if entity.kind != "File":
                        record = {
                            **self._entity_record(entity),
                            "change_id": change.change_id,
                            "commit_id": change.commit_id,
                            "status": change.status.value,
                            "relationship": "FILE_LEVEL_CANDIDATE",
                            "exact_symbol_diff": False,
                        }
                        record["evidence"] = self._git_evidence(
                            change.change_id, f"Git path change {file_path}"
                        )
                        records[f"{change.change_id}:{entity.entity_id}"] = record
        ordered = sorted(
            records.values(),
            key=lambda item: (item["file_path"], item["start_byte"], item["id"], item["change_id"]),
        )
        return self._result(
            request.snapshot_id,
            "find_changed_symbols",
            ordered,
            request.limit,
            ordering=("file_path", "start_byte", "symbol_id", "change_id"),
            diagnostics=("Changed paths are present, but no current symbols map to them.",)
            if changes and not ordered
            else (),
        )

    def find_observed_tests(
        self, request: GraphToolRequest, *, entity_ids: Sequence[str] = ()
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        wanted = set(entity_ids)
        known = set(self.entities)
        if wanted - known:
            return self._result(
                request.snapshot_id,
                "find_observed_tests",
                (),
                request.limit,
                ordering=("test_definition_id", "test_invocation_id", "entity_id"),
                diagnostics=("One or more requested symbol IDs are not in this snapshot.",),
            )
        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        invalid_records = 0
        for raw in self.runtime_records:
            if raw.get("snapshot_id") != request.snapshot_id:
                continue
            entity_id = str(raw.get("entity_id") or "")
            source_id = str(raw.get("source_entity_id") or "")
            observed_ids = {value for value in (entity_id, source_id) if value}
            if not observed_ids or observed_ids - known:
                invalid_records += 1
                continue
            if wanted and not observed_ids.intersection(wanted):
                continue
            test_definition_id = str(raw.get("test_definition_id") or "")
            invocation_id = str(raw.get("test_invocation_id") or "")
            run_id = str(raw.get("run_id") or "")
            materialization_id = str(raw.get("runtime_materialization_id") or "")
            environment_id = str(raw.get("runtime_environment_id") or "")
            group_key = (
                test_definition_id,
                invocation_id,
                run_id,
                materialization_id,
                environment_id,
            )
            record = grouped.setdefault(
                group_key,
                {
                    "test_definition_id": test_definition_id,
                    "test_invocation_id": invocation_id or None,
                    "node_id": raw.get("node_id"),
                    "run_id": run_id or None,
                    "runtime_materialization_id": materialization_id or None,
                    "runtime_environment_id": environment_id or None,
                    "entity_ids": set(),
                    "run_ids": set(),
                    "entry_ids": set(),
                    "observation_ids": set(),
                    "observation_kinds": set(),
                    "call_observations": {},
                    "evidence_type": "RUNTIME_OBSERVATION",
                    "verified_graph_path": None,
                },
            )
            record["entity_ids"].update(observed_ids)
            record["run_ids"].add(run_id)
            record_id = raw.get("observation_id") or raw.get("entry_id")
            if record_id:
                key = "observation_ids" if raw.get("observation_id") else "entry_ids"
                record[key].add(str(record_id))
            if raw.get("observation_kind"):
                record["observation_kinds"].add(str(raw["observation_kind"]))
            observation_id = str(raw.get("observation_id") or "")
            if raw.get("observation_kind") == "CALL_OBSERVATION" and observation_id:
                run_id = str(raw.get("run_id") or "")
                materialization_id = str(raw.get("runtime_materialization_id") or "")
                invocation_id = str(raw.get("test_invocation_id") or "")
                source_entity_id = str(raw.get("source_entity_id") or "")
                target_entity_id = str(raw.get("entity_id") or "")
                call_evidence = normalize_graph_evidence(
                    evidence_type=EvidenceType.RUNTIME_OBSERVATION,
                    snapshot_id=request.snapshot_id,
                    source_record_id=observation_id,
                    display_label=f"Observed call {source_entity_id} -> {target_entity_id}",
                    provenance_pointer=(
                        f"{request.snapshot_id}:materialization={materialization_id}:"
                        f"environment={raw.get('runtime_environment_id') or ''}:run={run_id}:"
                        f"invocation={invocation_id}:observation={observation_id}:"
                        f"site={raw.get('site_id') or ''}"
                    ),
                ).model_dump(mode="json")
                call = {
                    "snapshot_id": request.snapshot_id,
                    "runtime_materialization_id": materialization_id or None,
                    "runtime_environment_id": raw.get("runtime_environment_id"),
                    "test_definition_id": test_definition_id,
                    "test_invocation_id": invocation_id or None,
                    "run_id": run_id or None,
                    "observation_id": observation_id,
                    "source_entity_id": source_entity_id,
                    "target_entity_id": target_entity_id,
                    "site_id": raw.get("site_id"),
                    "evidence": call_evidence,
                }
                record["call_observations"][
                    (
                        run_id,
                        materialization_id,
                        invocation_id,
                        observation_id,
                        source_entity_id,
                        target_entity_id,
                        str(raw.get("site_id") or ""),
                    )
                ] = call
        records = []
        for record in grouped.values():
            record["call_observations"] = [
                record["call_observations"][key] for key in sorted(record["call_observations"])
            ]
            for field in (
                "entity_ids",
                "run_ids",
                "entry_ids",
                "observation_ids",
                "observation_kinds",
            ):
                record[field] = sorted(value for value in record[field] if value)
            record["entity_id"] = (
                record["entity_ids"][0] if len(record["entity_ids"]) == 1 else None
            )
            evidence_sources = sorted((*record["entry_ids"], *record["observation_ids"]))
            source_record_id = stable_id(
                request.snapshot_id,
                record["test_definition_id"],
                record["test_invocation_id"],
                evidence_sources,
                prefix="runtime_evidence_",
            )
            record["evidence"] = normalize_graph_evidence(
                evidence_type=EvidenceType.RUNTIME_OBSERVATION,
                snapshot_id=request.snapshot_id,
                source_record_id=source_record_id,
                display_label=f"Runtime observations for {record['test_definition_id']}",
                provenance_pointer=(
                    f"{request.snapshot_id}:materialization="
                    f"{record.get('runtime_materialization_id') or ''}:"
                    f"environment={record.get('runtime_environment_id') or ''}:"
                    f"runs={','.join(record['run_ids'])}:"
                    f"records={','.join(evidence_sources)}"
                ),
            ).model_dump(mode="json")
            records.append(record)
        records.sort(
            key=lambda item: (
                str(item.get("test_definition_id") or ""),
                str(item.get("test_invocation_id") or ""),
                tuple(item.get("entity_ids") or ()),
                str(item.get("runtime_materialization_id") or ""),
                str(item.get("runtime_environment_id") or ""),
                tuple(item.get("run_ids") or ()),
            )
        )
        return self._result(
            request.snapshot_id,
            "find_observed_tests",
            records,
            request.limit,
            ordering=(
                "test_definition_id",
                "test_invocation_id",
                "runtime_materialization_id",
                "runtime_environment_id",
                "run_ids",
            ),
            diagnostics=(
                (f"Ignored {invalid_records} runtime record(s) with unknown source identities.",)
                if invalid_records
                else (("No scoped runtime observations matched.",) if not records else ())
            ),
        )

    def build_test_plan(
        self, request: GraphToolRequest, changed_symbol_ids: Sequence[str] = ()
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        changed = tuple(sorted(set(changed_symbol_ids)))
        if not changed:
            changed = tuple(
                str(item["id"])
                for item in self.find_changed_symbols(request).results
                if item.get("id")
            )
        if any(item not in self.entities for item in changed):
            return self._result(
                request.snapshot_id,
                "build_test_plan",
                (),
                request.limit,
                ordering=("coverage_desc", "test_definition_id"),
                diagnostics=("Test plan contains a symbol outside the snapshot.",),
            )
        coverage: dict[str, set[str]] = defaultdict(set)
        invocations: dict[str, set[str]] = defaultdict(set)
        for raw in self.runtime_records:
            if raw.get("snapshot_id") != request.snapshot_id:
                continue
            definition_id = str(raw.get("test_definition_id") or "")
            entity_id = str(raw.get("entity_id") or raw.get("source_entity_id") or "")
            if definition_id and entity_id in changed:
                coverage[definition_id].add(entity_id)
                if raw.get("test_invocation_id"):
                    invocations[definition_id].add(str(raw["test_invocation_id"]))
        records = []
        for definition_id, symbol_ids in coverage.items():
            invocation_ids = sorted(invocations[definition_id])
            evidence = normalize_graph_evidence(
                evidence_type=EvidenceType.RUNTIME_OBSERVATION,
                snapshot_id=request.snapshot_id,
                source_record_id=stable_id(
                    request.snapshot_id, definition_id, invocation_ids, prefix="runtime_evidence_"
                ),
                display_label=f"Observed coverage for {definition_id}",
                provenance_pointer=(
                    f"{request.snapshot_id}:test={definition_id}:"
                    f"invocations={','.join(invocation_ids)}"
                ),
            ).model_dump(mode="json")
            records.append(
                {
                    "test_definition_id": definition_id,
                    "covered_changed_symbol_ids": sorted(symbol_ids),
                    "coverage_count": len(symbol_ids),
                    "test_invocation_ids": invocation_ids,
                    "selection_reason": "Observed runtime coverage of changed snapshot symbols.",
                    "evidence_type": "RUNTIME_OBSERVATION",
                    "evidence": evidence,
                }
            )
        records.sort(
            key=lambda item: (
                -item["coverage_count"] if isinstance(item["coverage_count"], int) else 0,
                str(item["test_definition_id"]),
            )
        )
        return self._result(
            request.snapshot_id,
            "build_test_plan",
            records,
            request.limit,
            ordering=("coverage_desc", "test_definition_id"),
            diagnostics=("No prior scoped runtime coverage is available for these symbols.",)
            if not records
            else (),
        )

    def _search(
        self,
        request: GraphToolRequest,
        tool: str,
        query: str,
        callback: Callable[[str, int], Sequence[Mapping[str, Any]]] | None,
        *,
        exclude_documents: bool = False,
    ) -> GraphToolResult:
        self._scope(request.snapshot_id)
        if callback is None:
            return self._result(
                request.snapshot_id,
                tool,
                (),
                request.limit,
                ordering=("score_desc", "chunk_id"),
                diagnostics=(f"{tool} provider is unavailable.",),
            )
        raw_records = callback(query, request.limit + 1)
        selected: dict[str, dict[str, Any]] = {}
        rejected = 0
        for raw in raw_records:
            if raw.get("snapshot_id") != request.snapshot_id:
                rejected += 1
                continue
            chunk_id = str(raw.get("chunk_id") or "")
            chunk = self.chunks.get(chunk_id)
            if chunk is None:
                rejected += 1
                continue
            if any(
                (
                    raw.get("source_node_id") != chunk.source_node_id,
                    raw.get("file_path") != chunk.file_path,
                    raw.get("content_hash") != chunk.content_hash,
                    raw.get("chunk_kind") != chunk.chunk_kind,
                    raw.get("text") != chunk.text,
                    raw.get("source_start_byte") != chunk.source_start_byte,
                    raw.get("source_end_byte") != chunk.source_end_byte,
                )
            ):
                rejected += 1
                continue
            if exclude_documents and chunk.chunk_kind == "DOCUMENT":
                continue
            record = {
                **dict(raw),
                "provenance_pointer": (
                    f"{chunk.snapshot_id}:{chunk.file_path}:"
                    f"{chunk.source_start_byte}:{chunk.source_end_byte}:{chunk.content_hash}"
                ),
            }
            record["evidence_type"] = (
                "SEMANTIC_PASSAGE" if tool == "search_semantic_chunks" else "LEXICAL_PASSAGE"
            )
            evidence_type = (
                EvidenceType.SEMANTIC_PASSAGE
                if tool == "search_semantic_chunks"
                else EvidenceType.LEXICAL_PASSAGE
            )
            record["evidence"] = self._passage_evidence(chunk, evidence_type)
            record["verified_graph_path"] = None
            selected.setdefault(chunk_id, record)
        records = sorted(
            selected.values(),
            key=lambda item: (-float(item.get("score") or 0), str(item["chunk_id"])),
        )
        return self._result(
            request.snapshot_id,
            tool,
            records,
            request.limit,
            ordering=("score_desc", "chunk_id"),
            diagnostics=(
                (f"Rejected {rejected} stale, cross-snapshot, or non-canonical chunk hit(s).",)
                if rejected
                else (("No retrieval passages matched the query.",) if not records else ())
            ),
        )

    def search_code_text(self, request: GraphToolRequest, query: str) -> GraphToolResult:
        return self._search(
            request,
            "search_code_text",
            query,
            self.lexical_search,
            exclude_documents=True,
        )

    def search_semantic_chunks(self, request: GraphToolRequest, query: str) -> GraphToolResult:
        return self._search(request, "search_semantic_chunks", query, self.semantic_search)

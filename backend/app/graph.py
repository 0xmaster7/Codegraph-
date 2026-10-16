from __future__ import annotations

import json
import logging
import re
import shutil
import time
from typing import Any

from falkordb import FalkorDB

from .config import Settings
from .identifiers import fulltext_terms, normalize_identifier
from .ids import stable_id
from .lifecycle import active_snapshot_ids, snapshot_lifecycle_lock
from .models import (
    IngestionJob,
    IngestionJobStatus,
    RepositoryInfo,
    RuntimeCallObservation,
    RuntimeContext,
    RuntimeDiagnostic,
    RuntimeEnvironment,
    RuntimeFunctionEntry,
    RuntimeRun,
    RuntimeRunStatus,
    SnapshotAnalysis,
    SnapshotStatus,
    TestDefinition,
    TestExecutionResult,
    TestInvocation,
)
from .schema import validate_analysis

logger = logging.getLogger(__name__)


def _runtime_float(value: object, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def _runtime_int(value: object, fallback: int) -> int:
    return value if isinstance(value, int) else fallback


def _safe_graph_name(value: str) -> str:
    return "".join(char if char.isalnum() or char == "_" else "_" for char in value)


class GraphWriteConflict(RuntimeError):
    """A snapshot-scoped graph record changed before a compare-and-swap write."""


class GraphStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        options: dict[str, Any] = {"host": settings.falkordb_host, "port": settings.falkordb_port}
        if settings.falkordb_username:
            options["username"] = settings.falkordb_username
        if settings.falkordb_password:
            options["password"] = settings.falkordb_password
        self.client = FalkorDB(**options)

    def ping(self) -> bool:
        return bool(self.client.connection.ping())

    def metadata_graph(self) -> Any:
        return self.client.select_graph("codegraph_metadata")

    def register_repository(self, repository: RepositoryInfo) -> str:
        self.bootstrap()
        graph = self.metadata_graph()
        graph.query(
            "MERGE (r:Repository {repository_id: $repository_id}) "
            "SET r.root = $root, r.remote = $remote, r.head_commit = $head_commit, "
            "r.is_dirty = $is_dirty, r.is_shallow = $is_shallow, "
            "r.is_detached = $is_detached",
            {
                "repository_id": repository.repository_id,
                "root": repository.root,
                "remote": repository.remote,
                "head_commit": repository.head_commit,
                "is_dirty": repository.is_dirty,
                "is_shallow": repository.is_shallow,
                "is_detached": repository.is_detached,
            },
        )
        return repository.repository_id

    def snapshot_status(self, snapshot_id: str) -> str | None:
        result = self.metadata_graph().query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) RETURN s.status",
            {"snapshot_id": snapshot_id},
        )
        return str(result.result_set[0][0]) if result.result_set else None

    def update_runtime_materialization(self, snapshot_id: str, materialization_id: str) -> None:
        """Advance the runtime-content pointer without mutating static snapshot identity."""
        with snapshot_lifecycle_lock():
            result = self.metadata_graph().query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "WHERE s.status = 'READY' SET s.runtime_materialization_id = $materialization_id "
                "RETURN s.snapshot_id",
                {"snapshot_id": snapshot_id, "materialization_id": materialization_id},
            )
            if not result.result_set:
                raise KeyError("Only a READY snapshot can receive runtime materialization")
            graph, _ = self.snapshot_graph(snapshot_id)
            graph.query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "SET s.runtime_materialization_id = $materialization_id",
                {"snapshot_id": snapshot_id, "materialization_id": materialization_id},
            )

    def list_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        result = self.metadata_graph().query(
            "MATCH (s:RepositorySnapshot) "
            "OPTIONAL MATCH (r:Repository {repository_id: s.repository_id}) "
            "RETURN s.snapshot_id AS snapshot_id, s.repository_id AS repository_id, "
            "r.root AS repository_root, s.status AS status, s.head_commit AS head_commit, "
            "s.created_at AS created_at, s.updated_at AS updated_at, "
            "s.ready_at AS ready_at, s.is_dirty AS is_dirty "
            "ORDER BY coalesce(s.updated_at, s.ready_at, s.created_at, 0) DESC, "
            "s.snapshot_id ASC LIMIT $limit",
            {"limit": limit},
        )
        return self._records(result)

    def get_investigation_memory(self, investigation_id: str) -> str | None:
        result = self.metadata_graph().query(
            "MATCH (i:Investigation {investigation_id: $investigation_id}) RETURN i.record_json",
            {"investigation_id": investigation_id},
        )
        return str(result.result_set[0][0]) if result.result_set else None

    def list_investigation_memories(self, snapshot_id: str, limit: int = 20) -> list[str]:
        if not 1 <= limit <= 100:
            raise ValueError("Investigation limit must be between 1 and 100")
        result = self.metadata_graph().query(
            "MATCH (i:Investigation {snapshot_id: $snapshot_id}) "
            "RETURN i.record_json ORDER BY i.updated_at DESC, i.investigation_id ASC LIMIT $limit",
            {"snapshot_id": snapshot_id, "limit": limit},
        )
        return [str(row[0]) for row in result.result_set if row and row[0] is not None]

    def save_investigation_memory(
        self,
        investigation_id: str,
        snapshot_id: str,
        record_json: str,
        created_at: float,
        updated_at: float,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Persist user investigation state and pin its immutable snapshot."""
        with snapshot_lifecycle_lock():
            payload = json.loads(record_json)
            if payload.get("investigation_id") != investigation_id:
                raise ValueError("Investigation payload identity does not match its record")
            if payload.get("snapshot_id") != snapshot_id:
                raise ValueError("Investigation payload snapshot does not match its record")
            repository_id = payload.get("repository_id")
            if not isinstance(repository_id, str) or not repository_id:
                raise ValueError("Investigation payload is missing its repository identity")
            payload_revision = payload.get("record_revision")
            if not isinstance(payload_revision, int) or payload_revision < 1:
                raise ValueError("Investigation payload is missing its next record revision")
            if expected_revision is None:
                expected_revision = payload_revision - 1
            if payload_revision != expected_revision + 1:
                raise ValueError("Investigation payload revision does not match compare-and-swap")
            existing_json = self.get_investigation_memory(investigation_id)
            if existing_json is not None:
                existing = json.loads(existing_json)
                if existing.get("snapshot_id") != snapshot_id:
                    raise ValueError("Investigation records cannot migrate between snapshots")
                if existing.get("repository_id") != repository_id:
                    raise ValueError("Investigation records cannot migrate between repositories")
            status = self.snapshot_status(snapshot_id)
            if status is None:
                raise KeyError("Snapshot is not registered in FalkorDB")
            if status != SnapshotStatus.READY.value:
                raise ValueError("Investigation memory requires a READY snapshot")
            result = self.metadata_graph().query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "WHERE s.status = 'READY' "
                "MERGE (i:Investigation {investigation_id: $investigation_id}) "
                "ON CREATE SET i.created_at = $created_at, i.record_revision = 0 "
                "WITH i, s WHERE (i.snapshot_id IS NULL OR i.snapshot_id = $snapshot_id) "
                "AND (i.repository_id IS NULL OR i.repository_id = $repository_id) "
                "AND coalesce(i.record_revision, 0) = $expected_revision "
                "MERGE (i)-[:PINS]->(s) "
                "SET i.repository_id = $repository_id, i.snapshot_id = $snapshot_id, "
                "i.record_json = $record_json, "
                "i.record_revision = $next_revision, "
                "i.updated_at = $updated_at "
                "RETURN i.record_revision",
                {
                    "snapshot_id": snapshot_id,
                    "repository_id": repository_id,
                    "investigation_id": investigation_id,
                    "record_json": record_json,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "expected_revision": expected_revision,
                    "next_revision": payload_revision,
                },
            )
            if not result.result_set:
                raise GraphWriteConflict(
                    "Investigation changed concurrently or its repository/snapshot binding differs"
                )

    def update_ingestion_job(self, job: IngestionJob, repository: RepositoryInfo) -> None:
        now = time.time()
        snapshot_status = {
            IngestionJobStatus.QUEUED: "DISCOVERED",
            IngestionJobStatus.MATERIALIZING: "MATERIALIZING",
            IngestionJobStatus.PARSING: "PARSING",
            IngestionJobStatus.SEMANTIC_INDEXING: "SEMANTIC_INDEXING",
            IngestionJobStatus.VALIDATING: "VALIDATING",
            IngestionJobStatus.PUBLISHING: "VALIDATING",
            IngestionJobStatus.COMPLETED: "VALIDATING",
            IngestionJobStatus.FAILED: "FAILED",
            IngestionJobStatus.CANCELLED: "CANCELLED",
            IngestionJobStatus.INTERRUPTED: "FAILED",
        }[job.status]
        self.metadata_graph().query(
            "MERGE (j:IngestionJob {job_id: $job_id}) SET "
            "j.snapshot_id = $snapshot_id, j.repository_id = $repository_id, "
            "j.status = $job_status, j.error = $error "
            "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) SET "
            "s.repository_id = $repository_id, s.job_id = $job_id, "
            "s.job_status = $job_status, "
            "s.status = CASE WHEN s.status = 'READY' THEN 'READY' ELSE $snapshot_status END, "
            "s.created_at = coalesce(s.created_at, $now), s.updated_at = $now, "
            "s.terminal_at = CASE WHEN $terminal THEN $now ELSE s.terminal_at END, "
            "s.ready_at = CASE WHEN s.status = 'READY' THEN coalesce(s.ready_at, $now) "
            "ELSE s.ready_at END, "
            "s.head_commit = $head_commit, s.is_dirty = $is_dirty, "
            "s.is_shallow = $is_shallow, s.is_detached = $is_detached",
            {
                "job_id": job.job_id,
                "snapshot_id": job.snapshot_id,
                "repository_id": repository.repository_id,
                "job_status": job.status.value,
                "snapshot_status": snapshot_status,
                "terminal": job.status
                in {
                    IngestionJobStatus.COMPLETED,
                    IngestionJobStatus.FAILED,
                    IngestionJobStatus.CANCELLED,
                    IngestionJobStatus.INTERRUPTED,
                },
                "now": now,
                "error": job.error,
                "head_commit": repository.head_commit or "",
                "is_dirty": repository.is_dirty,
                "is_shallow": repository.is_shallow,
                "is_detached": repository.is_detached,
            },
        )

    def recover_interrupted_ingestion_jobs(self) -> list[str]:
        metadata = self.metadata_graph()
        now = time.time()
        result = metadata.query(
            "MATCH (j:IngestionJob) WHERE j.status IN "
            "['QUEUED', 'MATERIALIZING', 'PARSING', 'SEMANTIC_INDEXING', "
            "'VALIDATING', 'PUBLISHING'] "
            "SET j.status = 'INTERRUPTED', j.error = 'Backend restarted during ingestion', "
            "j.updated_at = $now "
            "WITH j OPTIONAL MATCH (s:RepositorySnapshot {snapshot_id: j.snapshot_id}) "
            "SET s.status = 'FAILED', "
            "s.job_status = 'INTERRUPTED', s.terminal_at = $now, s.updated_at = $now "
            "RETURN j.job_id AS job_id",
            {"now": now},
        )
        recovered = [str(row[0]) for row in result.result_set]
        orphaned = metadata.query(
            "MATCH (s:RepositorySnapshot) WHERE s.job_status IN "
            "['QUEUED', 'MATERIALIZING', 'PARSING', 'SEMANTIC_INDEXING', "
            "'VALIDATING', 'PUBLISHING'] "
            "SET s.status = 'FAILED', "
            "s.job_status = 'INTERRUPTED', s.terminal_at = $now, s.updated_at = $now "
            "RETURN s.snapshot_id AS snapshot_id",
            {"now": now},
        )
        recovered.extend(str(row[0]) for row in orphaned.result_set)
        return recovered

    def snapshot_graph(self, snapshot_id: str) -> tuple[Any, str]:
        result = self.metadata_graph().query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
            "RETURN s.graph_name, s.status",
            {"snapshot_id": snapshot_id},
        )
        if not result.result_set:
            raise KeyError("Snapshot is not registered in FalkorDB")
        graph_name, status = result.result_set[0]
        if str(status) != "READY":
            raise KeyError("Only READY snapshots are available for graph queries")
        return self.client.select_graph(str(graph_name)), str(status)

    def resolve_exact(self, snapshot_id: str, query: str, limit: int = 20) -> dict[str, Any]:
        graph, status = self.snapshot_graph(snapshot_id)
        fields = (
            "n.id AS id, n.kind AS kind, n.name AS name, "
            "n.qualified_name AS qualified_name, n.file_path AS file_path, "
            "n.start_byte AS start_byte, n.end_byte AS end_byte, "
            "n.identifier_raw AS identifier_raw, "
            "n.identifier_search_text AS identifier_search_text"
        )
        for property_name, match_type in (
            ("id", "ID"),
            ("qualified_name", "QUALIFIED_NAME"),
            ("name", "NAME"),
        ):
            result = graph.query(
                "MATCH (n:SourceEntity {snapshot_id: $snapshot_id}) "
                f"WHERE n.{property_name} = $query "
                f"RETURN {fields} ORDER BY n.qualified_name, n.file_path, n.start_byte "
                "LIMIT $fetch_limit",
                {"snapshot_id": snapshot_id, "query": query, "fetch_limit": limit + 1},
            )
            if result.result_set:
                return self._symbol_resolution(query, match_type, status, result, limit)
        normalized = normalize_identifier(query)
        result = graph.query(
            "MATCH (n:SourceEntity {snapshot_id: $snapshot_id}) "
            "WHERE n.identifier_search_text = $normalized "
            f"RETURN {fields} ORDER BY n.qualified_name, n.file_path, n.start_byte "
            "LIMIT $fetch_limit",
            {"snapshot_id": snapshot_id, "normalized": normalized, "fetch_limit": limit + 1},
        )
        if result.result_set:
            return self._symbol_resolution(query, "NORMALIZED", status, result, limit)

        terms = fulltext_terms(query)
        if terms and self._has_symbol_fulltext_index(graph):
            result = graph.query(
                "CALL db.idx.fulltext.queryNodes('SourceEntity', $search_terms) "
                "YIELD node, score WITH node, score "
                "WHERE node.snapshot_id = $snapshot_id "
                "RETURN node.id AS id, node.kind AS kind, node.name AS name, "
                "node.qualified_name AS qualified_name, node.file_path AS file_path, "
                "node.start_byte AS start_byte, node.end_byte AS end_byte, "
                "node.identifier_raw AS identifier_raw, "
                "node.identifier_search_text AS identifier_search_text "
                "ORDER BY node.qualified_name, node.file_path, node.start_byte LIMIT $fetch_limit",
                {
                    "snapshot_id": snapshot_id,
                    "search_terms": terms,
                    "fetch_limit": limit + 1,
                },
            )
            if result.result_set:
                return self._symbol_resolution(query, "FULL_TEXT", status, result, limit)
        return {
            "query": query,
            "matched_by": None,
            "status": "NOT_FOUND",
            "snapshot_status": status,
            "results": [],
        }

    def _symbol_resolution(
        self, query: str, matched_by: str, status: str, result: Any, limit: int
    ) -> dict[str, Any]:
        records = self._records(result)
        truncated = len(records) > limit
        return {
            "query": query,
            "matched_by": matched_by,
            "status": "UNIQUE" if len(records) == 1 else "AMBIGUOUS",
            "snapshot_status": status,
            "results": records[:limit],
            "truncated": truncated,
        }

    def find_callers(
        self,
        snapshot_id: str,
        target_id: str,
        limit: int = 100,
        *,
        excluded_edge_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        graph, status = self.snapshot_graph(snapshot_id)
        params = {
            "snapshot_id": snapshot_id,
            "target_id": target_id,
            "excluded_edge_ids": list(excluded_edge_ids),
        }
        count_result = graph.query(
            "MATCH (site:CallSite)-[edge:BINDS_TO]->(target:SourceEntity {id: $target_id}) "
            "WHERE site.snapshot_id = $snapshot_id "
            "AND NOT edge.edge_id IN $excluded_edge_ids RETURN count(edge)",
            params,
        )
        count = int(count_result.result_set[0][0]) if count_result.result_set else 0
        result = graph.query(
            "MATCH (site:CallSite)-[edge:BINDS_TO]->(target:SourceEntity {id: $target_id}) "
            "WHERE site.snapshot_id = $snapshot_id AND NOT edge.edge_id IN $excluded_edge_ids "
            "RETURN site.id AS site_id, site.kind AS kind, site.expression AS expression, "
            "site.file_path AS file_path, site.start_byte AS start_byte, "
            "site.end_byte AS end_byte, edge.edge_id AS edge_id, "
            "edge.binding_state AS binding_state "
            "ORDER BY site.file_path, site.start_byte, site.id LIMIT $limit",
            {**params, "limit": limit},
        )
        return {
            "snapshot_id": snapshot_id,
            "snapshot_status": status,
            "target_id": target_id,
            "mode": "STRICT_STATIC_BINDING",
            "results": self._records(result),
            "candidate_count_before_limit": count,
            "result_limit": limit,
            "truncated": count > limit,
            "ordering": ["file_path", "start_byte", "site_id"],
            "limitations": (
                (["Only exact semantic bindings are returned."] if status != "READY" else [])
                + (
                    ["Relationships with replay integrity failures were excluded."]
                    if excluded_edge_ids
                    else []
                )
            ),
        }

    def find_references(
        self,
        snapshot_id: str,
        target_id: str,
        limit: int = 100,
        *,
        excluded_edge_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        graph, status = self.snapshot_graph(snapshot_id)
        params = {
            "snapshot_id": snapshot_id,
            "target_id": target_id,
            "excluded_edge_ids": list(excluded_edge_ids),
        }
        count_result = graph.query(
            "MATCH (site:ReferenceSite)-[edge:BINDS_TO]->(target {id: $target_id}) "
            "WHERE site.snapshot_id = $snapshot_id "
            "AND NOT edge.edge_id IN $excluded_edge_ids RETURN count(edge)",
            params,
        )
        count = int(count_result.result_set[0][0]) if count_result.result_set else 0
        result = graph.query(
            "MATCH (site:ReferenceSite)-[edge:BINDS_TO]->(target {id: $target_id}) "
            "WHERE site.snapshot_id = $snapshot_id AND NOT edge.edge_id IN $excluded_edge_ids "
            "RETURN site.id AS site_id, site.kind AS kind, site.expression AS expression, "
            "site.file_path AS file_path, site.start_byte AS start_byte, "
            "site.end_byte AS end_byte, edge.edge_id AS edge_id, "
            "edge.binding_state AS binding_state "
            "ORDER BY site.file_path, site.start_byte, site.id LIMIT $limit",
            {**params, "limit": limit},
        )
        return {
            "snapshot_id": snapshot_id,
            "snapshot_status": status,
            "target_id": target_id,
            "mode": "EXACT_SEMANTIC_BINDING",
            "results": self._records(result),
            "candidate_count_before_limit": count,
            "result_limit": limit,
            "truncated": count > limit,
            "ordering": ["file_path", "start_byte", "site_id"],
            "limitations": (
                (
                    ["Relationships with replay integrity failures were excluded."]
                    if excluded_edge_ids
                    else []
                )
                + (
                    ["The snapshot is not READY until all validation gates pass."]
                    if status != "READY"
                    else []
                )
            ),
        }

    def find_callees(
        self,
        snapshot_id: str,
        site_id: str,
        limit: int = 100,
        *,
        excluded_edge_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        graph, status = self.snapshot_graph(snapshot_id)
        params = {
            "snapshot_id": snapshot_id,
            "site_id": site_id,
            "excluded_edge_ids": list(excluded_edge_ids),
        }
        count_result = graph.query(
            "MATCH (site:CallSite {id: $site_id})-[edge:BINDS_TO]->(target) "
            "WHERE site.snapshot_id = $snapshot_id "
            "AND NOT edge.edge_id IN $excluded_edge_ids RETURN count(edge)",
            params,
        )
        count = int(count_result.result_set[0][0]) if count_result.result_set else 0
        result = graph.query(
            "MATCH (site:CallSite {id: $site_id})-[edge:BINDS_TO]->(target) "
            "WHERE site.snapshot_id = $snapshot_id AND NOT edge.edge_id IN $excluded_edge_ids "
            "RETURN target.id AS target_id, labels(target) AS target_labels, "
            "target.name AS name, target.qualified_name AS qualified_name, "
            "target.file_path AS file_path, edge.edge_id AS edge_id, "
            "edge.binding_state AS binding_state "
            "ORDER BY target.qualified_name, target.id LIMIT $limit",
            {**params, "limit": limit},
        )
        return {
            "snapshot_id": snapshot_id,
            "snapshot_status": status,
            "site_id": site_id,
            "mode": "EXACT_SEMANTIC_BINDING",
            "results": self._records(result),
            "candidate_count_before_limit": count,
            "result_limit": limit,
            "truncated": count > limit,
            "ordering": ["qualified_name", "target_id"],
            "limitations": (
                (
                    ["Relationships with replay integrity failures were excluded."]
                    if excluded_edge_ids
                    else []
                )
                + (
                    ["The snapshot is not READY until all validation gates pass."]
                    if status != "READY"
                    else []
                )
            ),
        }

    def runtime_tool_records(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Load invocation-scoped runtime evidence without presenting it as static truth."""
        graph, _ = self.snapshot_graph(snapshot_id)
        entries = graph.query(
            "MATCH (entry:RuntimeFunctionEntry {snapshot_id: $snapshot_id})-[:DURING]->"
            "(invocation:TestInvocation)-[:INSTANCE_OF]->(definition:TestDefinition) "
            "MATCH (entry)-[:OBSERVED_IN]->(run:RuntimeRun)-[:USES_ENVIRONMENT]->"
            "(environment:RuntimeEnvironment) "
            "WHERE entry.test_invocation_id IS NOT NULL "
            "RETURN entry.entry_id AS entry_id, entry.run_id AS run_id, "
            "entry.snapshot_id AS snapshot_id, entry.test_invocation_id AS test_invocation_id, "
            "environment.runtime_materialization_id AS runtime_materialization_id, "
            "entry.runtime_environment_id AS runtime_environment_id, "
            "definition.test_definition_id AS test_definition_id, "
            "invocation.node_id AS node_id, entry.entity_id AS entity_id, "
            "entry.observed_at AS observed_at, 'FUNCTION_ENTRY' AS observation_kind "
            "ORDER BY definition.test_definition_id, invocation.test_invocation_id, "
            "entry.entity_id, entry.run_id, entry.entry_id",
            {"snapshot_id": snapshot_id},
        )
        calls = graph.query(
            "MATCH (observation:RuntimeCallObservation {snapshot_id: $snapshot_id})-[:DURING]->"
            "(invocation:TestInvocation)-[:INSTANCE_OF]->(definition:TestDefinition) "
            "MATCH (observation)-[:OBSERVED_IN]->(run:RuntimeRun)-[:USES_ENVIRONMENT]->"
            "(environment:RuntimeEnvironment) "
            "WHERE observation.test_invocation_id IS NOT NULL "
            "RETURN observation.observation_id AS observation_id, observation.run_id AS run_id, "
            "observation.snapshot_id AS snapshot_id, "
            "observation.test_invocation_id AS test_invocation_id, "
            "environment.runtime_materialization_id AS runtime_materialization_id, "
            "observation.runtime_environment_id AS runtime_environment_id, "
            "definition.test_definition_id AS test_definition_id, "
            "invocation.node_id AS node_id, observation.target_entity_id AS entity_id, "
            "observation.source_entity_id AS source_entity_id, "
            "observation.site_id AS site_id, observation.observed_at AS observed_at, "
            "'CALL_OBSERVATION' AS observation_kind "
            "ORDER BY definition.test_definition_id, invocation.test_invocation_id, "
            "observation.target_entity_id, observation.run_id, observation.observation_id",
            {"snapshot_id": snapshot_id},
        )
        return [*self._records(entries), *self._records(calls)]

    @staticmethod
    def _records(result: Any) -> list[dict[str, Any]]:
        names = [str(item[0] if isinstance(item[0], str) else item[1]) for item in result.header]
        return [dict(zip(names, row, strict=True)) for row in result.result_set]

    def bootstrap(self) -> None:
        graph = self.metadata_graph()
        graph.query(
            "MERGE (m:CodeGraphMetadata {key: 'schema'}) ON CREATE SET m.schema_version = 'v1'"
        )
        self._ensure_indexes(
            graph,
            [
                ("Repository", "repository_id"),
                ("RepositorySnapshot", "snapshot_id"),
                ("RepositorySnapshot", "repository_id"),
                ("RepositorySnapshot", "status"),
                ("RepositorySnapshot", "ready_at"),
                ("IngestionJob", "job_id"),
                ("Developer", "developer_id"),
                ("Commit", "commit_id"),
                ("ChangedPath", "change_id"),
            ],
        )

    @staticmethod
    def _ensure_indexes(graph: Any, indexes: list[tuple[str, str]]) -> None:
        result = graph.query("CALL db.indexes()")
        existing = {
            (str(row[0]), str(property_name))
            for row in result.result_set
            if row[0] is not None and row[1]
            for property_name in row[1]
        }
        for label, property_name in indexes:
            if (label, property_name) in existing:
                continue
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{property_name})")

    def _ensure_snapshot_indexes(self, graph: Any) -> None:
        self._ensure_indexes(
            graph,
            [
                ("RepositorySnapshot", "snapshot_id"),
                ("SourceEntity", "id"),
                ("File", "id"),
                ("Module", "id"),
                ("Class", "id"),
                ("Function", "id"),
                ("Method", "id"),
                ("ReferenceSite", "id"),
                ("CallSite", "id"),
                ("ExternalSymbol", "id"),
                ("ProvenanceRecord", "provenance_id"),
                ("ProjectComponent", "component_id"),
                ("PackageDeclaration", "requirement_id"),
                ("PackageDistribution", "distribution_id"),
                ("RuntimeEnvironment", "runtime_environment_id"),
                ("RuntimeRun", "run_id"),
                ("TestDefinition", "test_definition_id"),
                ("TestInvocation", "test_invocation_id"),
                ("RuntimeFunctionEntry", "entry_id"),
                ("RuntimeCallObservation", "observation_id"),
                ("RuntimeDiagnostic", "diagnostic_id"),
                ("CodeChunk", "chunk_id"),
                ("DocumentChunk", "chunk_id"),
                ("DocumentSource", "id"),
            ],
        )
        self._ensure_symbol_fulltext_index(graph)
        self._ensure_retrieval_fulltext_indexes(graph)

    @staticmethod
    def _ensure_symbol_fulltext_index(graph: Any) -> None:
        required = {"identifier_raw", "identifier_search_text"}
        if GraphStore._has_symbol_fulltext_index(graph, required):
            return
        graph.query(
            "CREATE FULLTEXT INDEX FOR (n:SourceEntity) "
            "ON (n.identifier_raw, n.identifier_search_text)"
        )

    @staticmethod
    def _has_symbol_fulltext_index(graph: Any, required: set[str] | None = None) -> bool:
        properties = required or {"identifier_raw", "identifier_search_text"}
        catalog = graph.query("CALL db.indexes()")
        return any(
            len(row) >= 2
            and str(row[0]) == "SourceEntity"
            and properties <= {str(prop) for prop in row[1]}
            and "FULLTEXT" in repr(row)
            for row in catalog.result_set
        )

    @staticmethod
    def _ensure_retrieval_fulltext_indexes(graph: Any) -> None:
        catalog = graph.query("CALL db.indexes()")
        for label in ("CodeChunk", "DocumentChunk"):
            if any(
                len(row) >= 2
                and str(row[0]) == label
                and "text" in {str(prop) for prop in row[1]}
                and "FULLTEXT" in repr(row).upper()
                for row in catalog.result_set
            ):
                continue
            graph.query(f"CREATE FULLTEXT INDEX FOR (n:{label}) ON (n.text)")

    def persist_runtime_observations(
        self,
        analysis: SnapshotAnalysis,
        environment: RuntimeEnvironment,
        result: TestExecutionResult,
        definitions: list[TestDefinition],
        invocations: list[TestInvocation],
    ) -> RuntimeRun:
        if (
            environment.snapshot_id != analysis.snapshot_id
            or result.snapshot_id != analysis.snapshot_id
            or result.runtime_environment_id != environment.runtime_environment_id
        ):
            raise ValueError("Runtime observations cross snapshot or environment scope")
        entity_ids = {
            entity.entity_id
            for entity in analysis.entities
            if entity.kind in {"Function", "Method"}
        }
        definition_ids = {item.test_definition_id for item in definitions}
        invocation_ids = {item.test_invocation_id for item in invocations}
        site_ids = {item.site_id for item in analysis.sites}
        if any(item.snapshot_id != analysis.snapshot_id for item in definitions):
            raise ValueError("Test definition belongs to another snapshot")
        if any(
            item.snapshot_id != analysis.snapshot_id
            or item.test_definition_id not in definition_ids
            for item in invocations
        ):
            raise ValueError("Test invocation has an invalid snapshot or definition")
        for item in result.runtime_entries:
            if str(item.get("entity_id", "")) not in entity_ids:
                raise ValueError("Runtime entry references an entity outside this snapshot")
            invocation_id = item.get("test_invocation_id")
            if invocation_id is not None and str(invocation_id) not in invocation_ids:
                raise ValueError("Runtime entry references an invocation outside this run")
        for item in result.runtime_calls:
            if (
                str(item.get("source_entity_id", "")) not in entity_ids
                or str(item.get("target_entity_id", "")) not in entity_ids
            ):
                raise ValueError("Runtime call references an entity outside this snapshot")
            invocation_id = item.get("test_invocation_id")
            if invocation_id is not None and str(invocation_id) not in invocation_ids:
                raise ValueError("Runtime call references an invocation outside this run")
            site_id = item.get("site_id")
            if site_id is not None and (
                str(site_id) not in site_ids
                or next(site for site in analysis.sites if site.site_id == str(site_id)).context_id
                != str(item.get("source_entity_id", ""))
            ):
                raise ValueError("Runtime call references an incompatible static call site")
        for item in result.runtime_diagnostics:
            site_id = item.get("site_id")
            if site_id is not None and str(site_id) not in site_ids:
                raise ValueError("Runtime diagnostic references a site outside this snapshot")
        graph, _ = self.snapshot_graph(analysis.snapshot_id)
        self._ensure_snapshot_indexes(graph)
        run = RuntimeRun(
            run_id=result.run_id,
            snapshot_id=result.snapshot_id,
            runtime_environment_id=result.runtime_environment_id,
            status=RuntimeRunStatus.PERSISTING,
            started_at=result.started_at,
            ended_at=result.ended_at,
            subprocess_coverage="PARTIAL",
        )
        env_values = environment.model_dump(mode="json")
        graph.query(
            "MERGE (e:RuntimeEnvironment {runtime_environment_id: $runtime_environment_id}) "
            "SET e.snapshot_id = $snapshot_id, e.python_version = $python_version, "
            "e.platform = $platform, e.python_executable_hash = $python_executable_hash, "
            "e.sys_path_hash = $sys_path_hash, "
            "e.installed_distributions_hash = $installed_distributions_hash, "
            "e.source_manifest_hash = $source_manifest_hash, e.image_digest = $image_digest, "
            "e.runtime_materialization_id = $runtime_materialization_id",
            env_values,
        )
        graph.query(
            "MERGE (r:RuntimeRun {run_id: $run_id}) SET r.snapshot_id = $snapshot_id, "
            "r.runtime_environment_id = $runtime_environment_id, r.status = $status, "
            "r.started_at = $started_at, r.ended_at = $ended_at, "
            "r.subprocess_coverage = $subprocess_coverage "
            "WITH r MATCH (e:RuntimeEnvironment {runtime_environment_id: $environment_id}) "
            "MERGE (r)-[:USES_ENVIRONMENT]->(e)",
            {
                **run.model_dump(mode="json"),
                "environment_id": environment.runtime_environment_id,
            },
        )
        for definition in definitions:
            graph.query(
                "MERGE (d:TestDefinition {test_definition_id: $test_definition_id}) "
                "SET d.snapshot_id = $snapshot_id, d.file_path = $file_path, "
                "d.qualified_name = $qualified_name, d.pytest_name = $pytest_name",
                definition.model_dump(mode="json"),
            )
            matching = [
                entity
                for entity in analysis.entities
                if entity.entity_id in entity_ids
                and entity.file_path == definition.file_path
                and (
                    entity.qualified_name == definition.qualified_name
                    or entity.qualified_name.endswith(f".{definition.qualified_name}")
                )
            ]
            if len(matching) == 1:
                graph.query(
                    "MATCH (d:TestDefinition {test_definition_id: $definition_id}), "
                    "(e:SourceEntity {id: $entity_id}) MERGE (d)-[:BACKED_BY]->(e)",
                    {
                        "definition_id": definition.test_definition_id,
                        "entity_id": matching[0].entity_id,
                    },
                )
        for invocation in invocations:
            if invocation.snapshot_id != analysis.snapshot_id:
                raise ValueError("Test invocation belongs to another snapshot")
            graph.query(
                "MERGE (i:TestInvocation {test_invocation_id: $test_invocation_id}) "
                "SET i.snapshot_id = $snapshot_id, i.test_definition_id = $test_definition_id, "
                "i.node_id = $node_id, i.parameter_id = $parameter_id "
                "WITH i MATCH (d:TestDefinition {test_definition_id: $definition_id}) "
                "MERGE (i)-[:INSTANCE_OF]->(d)",
                {
                    **invocation.model_dump(mode="json"),
                    "definition_id": invocation.test_definition_id,
                },
            )
            graph.query(
                "MATCH (i:TestInvocation {test_invocation_id: $invocation_id}), "
                "(r:RuntimeRun {run_id: $run_id}) MERGE (i)-[:PART_OF]->(r)",
                {"invocation_id": invocation.test_invocation_id, "run_id": run.run_id},
            )
        for sequence, item in enumerate(result.runtime_entries):
            entity_id = str(item.get("entity_id", ""))
            invocation_id = item.get("test_invocation_id")
            context = RuntimeContext(str(item.get("context", "COLLECTION_OR_SESSION")))
            entry = RuntimeFunctionEntry(
                entry_id=stable_id(result.run_id, sequence, item, prefix="rentry_"),
                run_id=result.run_id,
                snapshot_id=result.snapshot_id,
                runtime_environment_id=environment.runtime_environment_id,
                test_invocation_id=str(invocation_id) if invocation_id else None,
                context=context,
                entity_id=entity_id,
                observed_at=_runtime_float(item.get("observed_at"), result.ended_at),
            )
            graph.query(
                "MERGE (e:RuntimeFunctionEntry {entry_id: $entry_id}) "
                "SET e.run_id = $run_id, e.snapshot_id = $snapshot_id, "
                "e.runtime_environment_id = $runtime_environment_id, "
                "e.test_invocation_id = $test_invocation_id, e.context = $context, "
                "e.entity_id = $entity_id, e.observed_at = $observed_at "
                "WITH e MATCH (r:RuntimeRun {run_id: $run_id}) "
                "MERGE (e)-[:OBSERVED_IN]->(r)",
                entry.model_dump(mode="json"),
            )
            graph.query(
                "MATCH (o:RuntimeFunctionEntry {entry_id: $entry_id}), "
                "(s:SourceEntity {id: $entity_id}) "
                "MERGE (o)-[:OBSERVED_ENTITY]->(s)",
                {"entry_id": entry.entry_id, "entity_id": entry.entity_id},
            )
            if entry.test_invocation_id:
                graph.query(
                    "MATCH (e:RuntimeFunctionEntry {entry_id: $entry_id}), "
                    "(i:TestInvocation {test_invocation_id: $invocation_id}) "
                    "MERGE (e)-[:DURING]->(i)",
                    {"entry_id": entry.entry_id, "invocation_id": entry.test_invocation_id},
                )
        for sequence, item in enumerate(result.runtime_calls):
            observation = RuntimeCallObservation(
                observation_id=stable_id(result.run_id, sequence, item, prefix="rcall_"),
                run_id=result.run_id,
                snapshot_id=result.snapshot_id,
                runtime_environment_id=environment.runtime_environment_id,
                test_invocation_id=(
                    str(item["test_invocation_id"]) if item.get("test_invocation_id") else None
                ),
                context=RuntimeContext(str(item.get("context", "COLLECTION_OR_SESSION"))),
                source_entity_id=str(item.get("source_entity_id", "")),
                target_entity_id=str(item.get("target_entity_id", "")),
                site_id=str(item["site_id"]) if item.get("site_id") else None,
                observed_at=_runtime_float(item.get("observed_at"), result.ended_at),
            )
            graph.query(
                "MERGE (o:RuntimeCallObservation {observation_id: $observation_id}) "
                "SET o.run_id = $run_id, o.snapshot_id = $snapshot_id, "
                "o.runtime_environment_id = $runtime_environment_id, "
                "o.test_invocation_id = $test_invocation_id, o.context = $context, "
                "o.source_entity_id = $source_entity_id, o.target_entity_id = $target_entity_id, "
                "o.site_id = $site_id, o.observed_at = $observed_at",
                observation.model_dump(mode="json"),
            )
            graph.query(
                "MATCH (o:RuntimeCallObservation {observation_id: $observation_id}), "
                "(source:SourceEntity {id: $source_entity_id}), "
                "(target:SourceEntity {id: $target_entity_id}) "
                "MERGE (o)-[:OBSERVED_FROM]->(source) "
                "MERGE (o)-[:OBSERVED_TO]->(target)",
                observation.model_dump(mode="json"),
            )
            if observation.site_id:
                graph.query(
                    "MATCH (o:RuntimeCallObservation {observation_id: $observation_id}), "
                    "(s:CallSite {id: $site_id}) MERGE (o)-[:OBSERVED_AT]->(s)",
                    {"observation_id": observation.observation_id, "site_id": observation.site_id},
                )
            graph.query(
                "MATCH (o:RuntimeCallObservation {observation_id: $observation_id}), "
                "(r:RuntimeRun {run_id: $run_id}) MERGE (o)-[:OBSERVED_IN]->(r)",
                {"observation_id": observation.observation_id, "run_id": run.run_id},
            )
            if observation.test_invocation_id:
                graph.query(
                    "MATCH (o:RuntimeCallObservation {observation_id: $observation_id}), "
                    "(i:TestInvocation {test_invocation_id: $invocation_id}) "
                    "MERGE (o)-[:DURING]->(i)",
                    {
                        "observation_id": observation.observation_id,
                        "invocation_id": observation.test_invocation_id,
                    },
                )
        for sequence, item in enumerate(result.runtime_diagnostics):
            diagnostic = RuntimeDiagnostic(
                diagnostic_id=stable_id(result.run_id, sequence, item, prefix="rdiag_"),
                run_id=result.run_id,
                snapshot_id=result.snapshot_id,
                code=str(item.get("code", "RUNTIME_DIAGNOSTIC")),
                message=str(item.get("message", "Runtime observation diagnostic.")),
                file_path=str(item.get("file_path", "")),
                qualified_name=str(item.get("qualified_name", "")),
                first_line=_runtime_int(item.get("first_line"), 1),
                site_id=str(item["site_id"]) if item.get("site_id") else None,
            )
            graph.query(
                "MERGE (d:RuntimeDiagnostic {diagnostic_id: $diagnostic_id}) "
                "SET d.run_id = $run_id, d.snapshot_id = $snapshot_id, d.code = $code, "
                "d.message = $message, d.file_path = $file_path, "
                "d.qualified_name = $qualified_name, d.first_line = $first_line",
                diagnostic.model_dump(mode="json"),
            )
            if diagnostic.site_id:
                graph.query(
                    "MATCH (d:RuntimeDiagnostic {diagnostic_id: $diagnostic_id}), "
                    "(s:CallSite {id: $site_id}) MERGE (d)-[:ABOUT_SITE]->(s)",
                    {"diagnostic_id": diagnostic.diagnostic_id, "site_id": diagnostic.site_id},
                )
            if diagnostic.code == "RUNTIME_MISMATCH" and diagnostic.site_id:
                edge_ids = {
                    binding.edge_id
                    for binding in analysis.bindings
                    if binding.site_id == diagnostic.site_id
                }
                edge_ids.update(
                    edge.edge_id
                    for edge in analysis.derived_edges
                    if edge.relation == "MAY_DISPATCH_TO" and edge.source_id == diagnostic.site_id
                )
                for edge_id in sorted(edge_ids):
                    graph.query(
                        "MATCH (p:ProvenanceRecord {edge_id: $edge_id}) "
                        "WHERE p.snapshot_id = $snapshot_id "
                        "SET p.review_state = 'RUNTIME_MISMATCH'",
                        {"edge_id": edge_id, "snapshot_id": analysis.snapshot_id},
                    )
            graph.query(
                "MATCH (d:RuntimeDiagnostic {diagnostic_id: $diagnostic_id}), "
                "(r:RuntimeRun {run_id: $run_id}) MERGE (d)-[:OBSERVED_IN]->(r)",
                {"diagnostic_id": diagnostic.diagnostic_id, "run_id": run.run_id},
            )
        graph.query(
            "MATCH (r:RuntimeRun {run_id: $run_id}) SET r.status = $status, r.ended_at = $ended_at",
            {"run_id": run.run_id, "status": result.status.value, "ended_at": result.ended_at},
        )
        return run.model_copy(update={"status": result.status})

    def recover_interrupted_runtime_persistence(self) -> list[str]:
        graph_names = self.client.list_graphs()
        recovered: list[str] = []
        for graph_name in graph_names:
            graph = self.client.select_graph(graph_name)
            result = graph.query(
                "MATCH (r:RuntimeRun {status: 'PERSISTING'}) "
                "SET r.status = 'FAILED' RETURN r.run_id"
            )
            recovered.extend(str(row[0]) for row in result.result_set)
        return recovered

    @staticmethod
    def _verify_snapshot_graph(graph: Any, analysis: SnapshotAnalysis) -> None:
        document_source_ids = {
            chunk.source_node_id
            for chunk in analysis.retrieval_chunks
            if chunk.chunk_kind == "DOCUMENT"
        }
        expected_nodes = (
            1
            + len(analysis.entities)
            + len(analysis.sites)
            + len(analysis.external_symbols)
            + len(analysis.provenance)
            + len(analysis.dependency_metadata.components)
            + len(analysis.dependency_metadata.requirements)
            + len(analysis.dependency_metadata.resolved_distributions)
            + len(analysis.retrieval_chunks)
            + len(document_source_ids)
        )
        nodes = graph.query(
            "MATCH (n) WHERE n.snapshot_id = $snapshot_id RETURN count(n)",
            {"snapshot_id": analysis.snapshot_id},
        )
        actual_nodes = int(nodes.result_set[0][0]) if nodes.result_set else 0
        if actual_nodes != expected_nodes:
            raise RuntimeError(
                f"Snapshot node count mismatch: expected {expected_nodes}, found {actual_nodes}"
            )
        assigned_requirements = sum(
            requirement.component_id is not None
            for requirement in analysis.dependency_metadata.requirements
        )
        expected_edges = (
            len(analysis.entities)
            + len(analysis.sites)
            + len(analysis.bindings)
            + len(analysis.imports)
            + len(analysis.derived_edges)
            + len(analysis.dependency_metadata.components)
            + assigned_requirements
            + len(analysis.dependency_metadata.resolved_distributions)
            + (2 * len(analysis.retrieval_chunks))
        )
        edges = graph.query("MATCH ()-[r]->() RETURN count(r), count(DISTINCT r.edge_id)")
        if not edges.result_set:
            actual_edges = distinct_edges = 0
        else:
            actual_edges, distinct_edges = (int(value or 0) for value in edges.result_set[0])
        if actual_edges != expected_edges or distinct_edges != expected_edges:
            raise RuntimeError(
                "Snapshot edge count mismatch: "
                f"expected {expected_edges} unique edges, found {actual_edges}/{distinct_edges}"
            )
        stored_imports = graph.query(
            "MATCH ()-[r:IMPORTS]->() WHERE r.snapshot_id = $snapshot_id "
            "RETURN count(r), count(DISTINCT r.edge_id)",
            {"snapshot_id": analysis.snapshot_id},
        )
        import_count, distinct_imports = (
            (int(value or 0) for value in stored_imports.result_set[0])
            if stored_imports.result_set
            else (0, 0)
        )
        if import_count != len(analysis.imports) or distinct_imports != len(analysis.imports):
            raise RuntimeError("Persisted IMPORTS relationship count does not match analysis")

    def _write_snapshot(self, analysis: SnapshotAnalysis) -> str:
        validate_analysis(analysis)
        self.bootstrap()
        graph_name = _safe_graph_name(
            f"code_{analysis.repository.repository_id}_{analysis.snapshot_id}"
        )
        graph = self.client.select_graph(graph_name)
        graph.query(
            "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
            "SET s.repository_id = $repository_id, s.status = 'VALIDATING', "
            "s.head_commit = $head_commit, s.entity_count = $entity_count, "
            "s.site_count = $site_count, s.environment_id = $environment_id",
            {
                "snapshot_id": analysis.snapshot_id,
                "repository_id": analysis.repository.repository_id,
                "head_commit": analysis.repository.head_commit or "",
                "entity_count": len(analysis.entities),
                "site_count": len(analysis.sites),
                "environment_id": (
                    analysis.analysis_environment.environment_id
                    if analysis.analysis_environment
                    else ""
                ),
            },
        )
        for entity in analysis.entities:
            entity_label = entity.kind
            graph.query(
                f"MERGE (n:SourceEntity:{entity_label} {{id: $id}}) "
                "SET n.kind = $kind, n.name = $name, "
                "n.qualified_name = $qualified_name, n.file_path = $file_path, "
                "n.identifier_raw = $identifier_raw, "
                "n.identifier_search_text = $identifier_search_text, "
                "n.start_byte = $start_byte, n.end_byte = $end_byte, n.snapshot_id = $snapshot_id",
                {
                    "id": entity.entity_id,
                    "kind": entity.kind,
                    "name": entity.name,
                    "qualified_name": entity.qualified_name,
                    "file_path": entity.file_path,
                    "identifier_raw": entity.identifier_raw,
                    "identifier_search_text": entity.identifier_search_text,
                    "start_byte": entity.span.start_byte,
                    "end_byte": entity.span.end_byte,
                    "snapshot_id": analysis.snapshot_id,
                },
            )
            if entity.parent_id:
                relation = (
                    "DECLARES_MODULE"
                    if entity.kind == "Module"
                    else ("DEFINES_METHOD" if entity.kind == "Method" else "DEFINES")
                )
                edge_id = stable_id(
                    "static",
                    relation,
                    entity.parent_id,
                    entity.entity_id,
                    entity.span.start_byte,
                    prefix="edge_",
                )
                graph.query(
                    f"MATCH (p:SourceEntity {{id: $parent_id}}), (n:SourceEntity {{id: $id}}) "
                    f"MERGE (p)-[:{relation} {{edge_id: $edge_id}}]->(n)",
                    {"parent_id": entity.parent_id, "id": entity.entity_id, "edge_id": edge_id},
                )
            if entity.kind == "File":
                edge_id = stable_id(
                    "static", "CONTAINS", analysis.snapshot_id, entity.entity_id, 0, prefix="edge_"
                )
                graph.query(
                    "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}), "
                    "(f:SourceEntity {id: $file_id}) "
                    "MERGE (s)-[:CONTAINS {edge_id: $edge_id}]->(f)",
                    {
                        "snapshot_id": analysis.snapshot_id,
                        "file_id": entity.entity_id,
                        "edge_id": edge_id,
                    },
                )
        document_sources: set[str] = set()
        for chunk in analysis.retrieval_chunks:
            is_document = chunk.chunk_kind == "DOCUMENT"
            chunk_label = "DocumentChunk" if is_document else "CodeChunk"
            if is_document and chunk.source_node_id not in document_sources:
                document_sources.add(chunk.source_node_id)
                source_hash = next(
                    entry.content_hash
                    for entry in analysis.manifest
                    if entry.path == chunk.file_path
                )
                graph.query(
                    "MERGE (d:DocumentSource {id: $source_id}) "
                    "SET d.snapshot_id = $snapshot_id, d.file_path = $file_path, "
                    "d.content_hash = $content_hash",
                    {
                        "source_id": chunk.source_node_id,
                        "snapshot_id": analysis.snapshot_id,
                        "file_path": chunk.file_path,
                        "content_hash": source_hash,
                    },
                )
            graph.query(
                f"MERGE (c:{chunk_label} {{chunk_id: $chunk_id}}) "
                "SET c.snapshot_id = $snapshot_id, c.source_node_id = $source_node_id, "
                "c.file_path = $file_path, c.chunk_kind = $chunk_kind, "
                "c.start_byte = $start_byte, c.end_byte = $end_byte, "
                "c.content_hash = $content_hash, c.text = $text, "
                "c.chunk_strategy_version = $strategy_version",
                {
                    "chunk_id": chunk.chunk_id,
                    "snapshot_id": analysis.snapshot_id,
                    "source_node_id": chunk.source_node_id,
                    "file_path": chunk.file_path,
                    "chunk_kind": chunk.chunk_kind,
                    "start_byte": chunk.source_start_byte,
                    "end_byte": chunk.source_end_byte,
                    "content_hash": chunk.content_hash,
                    "text": chunk.text,
                    "strategy_version": chunk.chunk_strategy_version,
                },
            )
            source_label = "DocumentSource" if is_document else "SourceEntity"
            relation_id = stable_id(
                "retrieval",
                "EVIDENCE_FOR",
                chunk.chunk_id,
                chunk.source_node_id,
                prefix="edge_",
            )
            snapshot_link_id = stable_id(
                "retrieval",
                "HAS_RETRIEVAL_EVIDENCE",
                analysis.snapshot_id,
                chunk.chunk_id,
                prefix="edge_",
            )
            graph.query(
                f"MATCH (c:{chunk_label} {{chunk_id: $chunk_id}}), "
                f"(s:{source_label} {{id: $source_id}}) "
                "MERGE (c)-[:EVIDENCE_FOR {edge_id: $relation_id}]->(s) "
                "WITH c MATCH (snapshot:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "MERGE (snapshot)-[:HAS_RETRIEVAL_EVIDENCE "
                "{edge_id: $snapshot_link_id}]->(c)",
                {
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source_node_id,
                    "snapshot_id": analysis.snapshot_id,
                    "relation_id": relation_id,
                    "snapshot_link_id": snapshot_link_id,
                },
            )
        for site in analysis.sites:
            site_label = (
                "CallSite:ReferenceSite" if site.kind.value == "CALL_CALLEE" else "ReferenceSite"
            )
            edge_id = stable_id(
                "static",
                "OCCURS_IN",
                site.site_id,
                site.context_id,
                site.span.start_byte,
                prefix="edge_",
            )
            graph.query(
                f"MERGE (n:{site_label} {{id: $id}}) "
                "SET n.kind = $kind, n.expression = $expression, "
                "n.file_path = $file_path, n.start_byte = $start_byte, n.end_byte = $end_byte, "
                "n.context_id = $context_id, n.snapshot_id = $snapshot_id",
                {
                    "id": site.site_id,
                    "kind": site.kind.value,
                    "expression": site.expression,
                    "file_path": site.file_path,
                    "start_byte": site.span.start_byte,
                    "end_byte": site.span.end_byte,
                    "context_id": site.context_id,
                    "snapshot_id": analysis.snapshot_id,
                },
            )
            graph.query(
                "MATCH (s:ReferenceSite {id: $site_id}), (c:SourceEntity {id: $context_id}) "
                "MERGE (s)-[:OCCURS_IN {edge_id: $edge_id}]->(c)",
                {"site_id": site.site_id, "context_id": site.context_id, "edge_id": edge_id},
            )
        for state in analysis.dispatch:
            graph.query(
                "MATCH (c:CallSite {id: $site_id}) SET c.dispatch_state = $state, "
                "c.dispatch_candidates = $candidates, c.mro_entity_ids = $mro, "
                "c.mro_qualified_names = $mro_names",
                {
                    "site_id": state.site_id,
                    "state": state.state.value,
                    "candidates": state.candidate_entity_ids,
                    "mro": state.mro_entity_ids,
                    "mro_names": state.mro_qualified_names,
                },
            )
        for external in analysis.external_symbols:
            graph.query(
                "MERGE (e:ExternalSymbol {id: $id}) SET e.semantic_symbol = $semantic_symbol, "
                "e.environment_id = $environment_id, e.module_name = $module_name, "
                "e.distribution_name = $distribution_name, "
                "e.distribution_version = $distribution_version, "
                "e.external_kind = $external_kind, "
                "e.snapshot_id = $snapshot_id",
                {
                    "id": external.external_symbol_id,
                    "semantic_symbol": external.semantic_symbol,
                    "environment_id": external.environment_id,
                    "module_name": external.module_name,
                    "distribution_name": external.distribution_name,
                    "distribution_version": external.distribution_version,
                    "external_kind": external.external_kind,
                    "snapshot_id": analysis.snapshot_id,
                },
            )
        for binding in analysis.bindings:
            target_label = "SourceEntity" if binding.target_entity_id else "ExternalSymbol"
            target_id = binding.target_entity_id or binding.external_symbol_id
            graph.query(
                f"MATCH (s:ReferenceSite {{id: $site_id}}), (t:{target_label} {{id: $target_id}}) "
                "MERGE (s)-[:BINDS_TO {edge_id: $edge_id, binding_state: $binding_state, "
                "resolver: $resolver, scip_symbol: $scip_symbol}]->(t)",
                {
                    "site_id": binding.site_id,
                    "target_id": target_id,
                    "edge_id": binding.edge_id,
                    "binding_state": binding.binding_state,
                    "resolver": binding.resolver,
                    "scip_symbol": binding.scip_symbol,
                },
            )
        for item in analysis.imports:
            target_id = item.target_module_id or item.target_external_symbol_id
            target_label = "SourceEntity" if item.target_module_id else "ExternalSymbol"
            source_site = next(site for site in analysis.sites if site.site_id == item.site_id)
            graph.query(
                "MATCH (source:SourceEntity {id: $source_id, snapshot_id: $snapshot_id}), "
                f"(target:{target_label} {{id: $target_id}}) "
                "MERGE (source)-[r:IMPORTS {edge_id: $edge_id}]->(target) "
                "SET r.snapshot_id = $snapshot_id, r.site_id = $site_id, "
                "r.imported_module = $imported_module, r.resolver = $resolver, "
                "r.file_path = $file_path, r.start_byte = $start_byte, r.end_byte = $end_byte, "
                "r.start_line = $start_line, r.start_column = $start_column, "
                "r.end_line = $end_line, r.end_column = $end_column",
                {
                    "source_id": item.source_module_id,
                    "snapshot_id": analysis.snapshot_id,
                    "target_id": target_id,
                    "edge_id": item.edge_id,
                    "site_id": item.site_id,
                    "imported_module": item.imported_module,
                    "resolver": item.resolver,
                    "file_path": source_site.file_path,
                    "start_byte": item.span.start_byte,
                    "end_byte": item.span.end_byte,
                    "start_line": item.span.start_line,
                    "start_column": item.span.start_column,
                    "end_line": item.span.end_line,
                    "end_column": item.span.end_column,
                },
            )
        for edge in analysis.derived_edges:
            if edge.relation not in {"INHERITS", "OVERRIDES", "MAY_DISPATCH_TO"}:
                continue
            source_label = (
                "CallSite"
                if edge.source_id in {item.site_id for item in analysis.sites}
                else "SourceEntity"
            )
            target_label = (
                "ExternalSymbol"
                if edge.target_id in {item.external_symbol_id for item in analysis.external_symbols}
                else "SourceEntity"
            )
            graph.query(
                f"MATCH (s:{source_label} {{id: $source_id}}), "
                f"(t:{target_label} {{id: $target_id}}) "
                f"MERGE (s)-[r:{edge.relation} {{edge_id: $edge_id}}]->(t) "
                "SET r.rule_id = $rule_id, r.premise_edge_ids = $premises",
                {
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "edge_id": edge.edge_id,
                    "rule_id": edge.rule_id,
                    "premises": edge.premise_edge_ids,
                },
            )
        for provenance in analysis.provenance:
            graph.query(
                "MERGE (p:ProvenanceRecord {provenance_id: $provenance_id}) SET "
                "p.edge_id = $edge_id, p.snapshot_id = $snapshot_id, p.file_path = $file_path, "
                "p.file_content_hash = $file_content_hash, p.start_byte = $start_byte, "
                "p.end_byte = $end_byte, p.start_line = $start_line, "
                "p.start_column = $start_column, p.end_line = $end_line, "
                "p.end_column = $end_column, "
                "p.source_coordinate_encoding = $source_coordinate_encoding, "
                "p.repository_id = $repository_id, p.analysis_environment_id = $environment_id, "
                "p.scip_document = $scip_document, p.scip_symbol = $scip_symbol, "
                "p.scip_position_encoding = $scip_position_encoding, "
                "p.created_at = coalesce(p.created_at, $created_at), "
                "p.syntax_node_type = $syntax_node_type, "
                "p.extractor_name = $extractor_name, p.extractor_version = $extractor_version, "
                "p.rule_id = $rule_id, p.premise_edge_ids = $premises, "
                "p.replay_scope = $replay_scope, p.replay_state = $replay_state, "
                "p.review_state = $review_state",
                {
                    "provenance_id": provenance.provenance_id,
                    "edge_id": provenance.edge_id,
                    "snapshot_id": provenance.snapshot_id,
                    "file_path": provenance.file_path,
                    "file_content_hash": provenance.file_content_hash,
                    "start_byte": provenance.span.start_byte,
                    "end_byte": provenance.span.end_byte,
                    "start_line": provenance.span.start_line,
                    "start_column": provenance.span.start_column,
                    "end_line": provenance.span.end_line,
                    "end_column": provenance.span.end_column,
                    "source_coordinate_encoding": provenance.source_coordinate_encoding,
                    "repository_id": analysis.repository.repository_id,
                    "environment_id": (
                        analysis.analysis_environment.environment_id
                        if analysis.analysis_environment
                        else ""
                    ),
                    "scip_document": provenance.scip_document or "",
                    "scip_symbol": provenance.scip_symbol or "",
                    "scip_position_encoding": provenance.scip_position_encoding or "",
                    "created_at": time.time(),
                    "syntax_node_type": provenance.syntax_node_type,
                    "extractor_name": provenance.extractor_name,
                    "extractor_version": provenance.extractor_version,
                    "rule_id": provenance.rule_id,
                    "premises": provenance.premise_edge_ids,
                    "replay_scope": provenance.replay_scope.value,
                    "replay_state": provenance.replay_state.value,
                    "review_state": provenance.review_state.value,
                },
            )
        graph.query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) SET s.status = 'VALIDATING'",
            {"snapshot_id": analysis.snapshot_id},
        )
        metadata = self.metadata_graph()
        metadata.query(
            "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) SET "
            "s.repository_id = $repository_id, s.graph_name = $graph_name, "
            "s.status = 'VALIDATING', s.runtime_materialization_id = $runtime_materialization_id, "
            "s.head_commit = $head_commit",
            {
                "snapshot_id": analysis.snapshot_id,
                "repository_id": analysis.repository.repository_id,
                "graph_name": graph_name,
                "runtime_materialization_id": analysis.runtime_materialization_id or "",
                "head_commit": analysis.repository.head_commit or "",
            },
        )
        return graph_name

    def publish_snapshot(self, analysis: SnapshotAnalysis, job_id: str | None = None) -> str:
        with snapshot_lifecycle_lock():
            graph_name = self._publish_snapshot_locked(analysis, job_id)
            try:
                self.garbage_collect_snapshots()
            except Exception:
                logger.warning(
                    "Snapshot published but garbage collection did not finish", exc_info=True
                )
            return graph_name

    @staticmethod
    def _embedding_property(spec_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", spec_id):
            raise ValueError("Embedding configuration identity must be a SHA-256 digest")
        return f"embedding_{spec_id[:20]}"

    def store_chunk_embeddings(self, snapshot_id: str, measurement: Any) -> None:
        """Persist provider-scoped vectors without turning similarity into graph facts."""
        from .embeddings import EmbeddedVector

        if self.snapshot_status(snapshot_id) != "READY":
            raise KeyError("Embeddings can only be added to a READY snapshot")
        graph, _ = self.snapshot_graph(snapshot_id)
        property_name = self._embedding_property(measurement.spec.identity)
        dimension = int(measurement.spec.dimensions)
        chunk_labels = self._analysis_chunks(snapshot_id)
        for vector in measurement.vectors:
            if not isinstance(vector, EmbeddedVector):
                vector = EmbeddedVector.model_validate(vector)
            if vector.snapshot_id != snapshot_id or vector.spec_id != measurement.spec.identity:
                raise ValueError("Embedding crosses snapshot or provider configuration scope")
            if len(vector.vector) != dimension:
                raise ValueError("Embedding dimension does not match its provider configuration")
            chunk_label = chunk_labels.get(vector.chunk_id)
            if chunk_label is None:
                raise ValueError("Embedding references a chunk outside this snapshot")
            current = graph.query(
                f"MATCH (c:{chunk_label} {{chunk_id: $chunk_id}}) "
                "WHERE c.snapshot_id = $snapshot_id "
                f"RETURN c.content_hash, c.{property_name}",
                {"chunk_id": vector.chunk_id, "snapshot_id": snapshot_id},
            )
            if not current.result_set:
                raise ValueError("Embedding references a missing canonical chunk")
            stored_content_hash = str(current.result_set[0][0] or "")
            if stored_content_hash != vector.content_hash:
                raise ValueError("Embedding content hash does not match the canonical chunk")
            current_vector = current.result_set[0][1]
            if current_vector is not None:
                if tuple(float(value) for value in current_vector) != vector.vector:
                    raise ValueError(
                        "An immutable embedding already exists for this chunk/provider identity"
                    )
                continue
            graph.query(
                f"MATCH (c:{chunk_label} {{chunk_id: $chunk_id}}) "
                f"WHERE c.snapshot_id = $snapshot_id SET c.{property_name} = vecf32($vector)",
                {
                    "chunk_id": vector.chunk_id,
                    "snapshot_id": snapshot_id,
                    "vector": list(vector.vector),
                },
            )
        # FalkorDB builds a vector index from the values present when the index
        # is created. Populate chunk vectors first so the initial index includes
        # every embedding from this immutable measurement.
        indexes = graph.query("CALL db.indexes()")
        for label in ("CodeChunk", "DocumentChunk"):
            if not any(
                len(row) >= 2
                and row[0] == label
                and property_name in row[1]
                and "VECTOR" in repr(row).upper()
                for row in indexes.result_set
            ):
                graph.query(
                    f"CREATE VECTOR INDEX FOR (c:{label}) ON (c.{property_name}) "
                    f"OPTIONS {{dimension: {dimension}, similarityFunction: 'cosine', "
                    "efRuntime: 200}"
                )

    def _analysis_chunks(self, snapshot_id: str) -> dict[str, str]:
        graph, _ = self.snapshot_graph(snapshot_id)
        documents = graph.query(
            "MATCH (c:DocumentChunk {snapshot_id: $snapshot_id}) RETURN c.chunk_id",
            {"snapshot_id": snapshot_id},
        )
        code = graph.query(
            "MATCH (c:CodeChunk {snapshot_id: $snapshot_id}) RETURN c.chunk_id",
            {"snapshot_id": snapshot_id},
        )
        return {
            **{str(row[0]): "DocumentChunk" for row in documents.result_set},
            **{str(row[0]): "CodeChunk" for row in code.result_set},
        }

    def query_chunk_embeddings(
        self, snapshot_id: str, spec_id: str, vector: list[float], *, limit: int = 10
    ) -> list[dict[str, Any]]:
        if self.snapshot_status(snapshot_id) != "READY":
            raise KeyError("Only READY snapshots can be queried")
        graph, _ = self.snapshot_graph(snapshot_id)
        property_name = self._embedding_property(spec_id)
        results: list[dict[str, Any]] = []
        for label in ("CodeChunk", "DocumentChunk"):
            queried = graph.query(
                f"CALL db.idx.vector.queryNodes('{label}', '{property_name}', "
                "$limit, vecf32($vector)) YIELD node, score "
                "WHERE node.snapshot_id = $snapshot_id "
                "RETURN node.chunk_id, node.snapshot_id, node.file_path, node.source_node_id, "
                "node.chunk_kind, node.text, node.content_hash, node.start_byte, "
                "node.end_byte, score ORDER BY score ASC",
                {
                    "limit": limit,
                    "vector": vector,
                    "snapshot_id": snapshot_id,
                },
            )
            for row in queried.result_set:
                results.append(
                    {
                        "chunk_id": row[0],
                        "snapshot_id": row[1],
                        "file_path": row[2],
                        "source_node_id": row[3],
                        "chunk_kind": row[4],
                        "text": row[5],
                        "content_hash": row[6],
                        "source_start_byte": int(row[7]),
                        "source_end_byte": int(row[8]),
                        # FalkorDB returns cosine distance: zero is the closest result.
                        "score": 1.0 - float(row[9]),
                        "type": "SEMANTIC_PASSAGE",
                        "verified_graph_path": None,
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["chunk_id"]))
        return results[:limit]

    def query_chunk_fulltext(
        self, snapshot_id: str, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return snapshot-scoped lexical passage evidence from code and docs."""
        if limit < 1:
            raise ValueError("Passage result limit must be positive")
        if self.snapshot_status(snapshot_id) != "READY":
            raise KeyError("Only READY snapshots can be queried")
        terms = fulltext_terms(query)
        if not terms:
            return []
        graph, _ = self.snapshot_graph(snapshot_id)
        self._ensure_retrieval_fulltext_indexes(graph)
        results: dict[str, dict[str, Any]] = {}
        for label in ("CodeChunk", "DocumentChunk"):
            queried = graph.query(
                f"CALL db.idx.fulltext.queryNodes('{label}', $search_terms) "
                "YIELD node, score WHERE node.snapshot_id = $snapshot_id "
                "RETURN node.chunk_id, node.snapshot_id, node.source_node_id, "
                "node.file_path, node.text, node.content_hash, node.chunk_kind, "
                "node.start_byte, node.end_byte, score "
                "ORDER BY score DESC, node.chunk_id LIMIT $limit",
                {"search_terms": terms, "snapshot_id": snapshot_id, "limit": limit},
            )
            for row in queried.result_set:
                chunk_id = str(row[0])
                results[chunk_id] = {
                    "chunk_id": chunk_id,
                    "snapshot_id": str(row[1]),
                    "source_node_id": str(row[2]),
                    "file_path": str(row[3]),
                    "text": str(row[4]),
                    "content_hash": str(row[5]),
                    "chunk_kind": str(row[6]),
                    "source_start_byte": int(row[7]),
                    "source_end_byte": int(row[8]),
                    "score": float(row[9]),
                    "type": "LEXICAL_PASSAGE",
                    "verified_graph_path": None,
                }
        ranked = sorted(results.values(), key=lambda item: (-item["score"], item["chunk_id"]))
        return ranked[:limit]

    def _publish_snapshot_locked(
        self, analysis: SnapshotAnalysis, job_id: str | None = None
    ) -> str:
        validate_analysis(analysis)
        self.bootstrap()
        graph_name = _safe_graph_name(
            f"code_{analysis.repository.repository_id}_{analysis.snapshot_id}"
        )
        metadata = self.metadata_graph()
        existing = metadata.query(
            "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
            "RETURN s.graph_name, s.status",
            {"snapshot_id": analysis.snapshot_id},
        )
        if existing.result_set and str(existing.result_set[0][1]) == "READY":
            now = time.time()
            metadata.query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "SET s.ready_at = coalesce(s.ready_at, $now), "
                "s.updated_at = coalesce(s.updated_at, $now), "
                "s.job_status = 'COMPLETED'",
                {"snapshot_id": analysis.snapshot_id, "now": now},
            )
            analysis.status = SnapshotStatus.READY
            return str(existing.result_set[0][0])

        now = time.time()
        metadata.query(
            "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) SET "
            "s.repository_id = $repository_id, s.graph_name = $graph_name, "
            "s.status = 'VALIDATING', s.head_commit = $head_commit, "
            "s.is_shallow = $is_shallow, s.is_detached = $is_detached, "
            "s.created_at = coalesce(s.created_at, $now), s.updated_at = $now, "
            "s.ready_at = NULL, s.terminal_at = NULL",
            {
                "snapshot_id": analysis.snapshot_id,
                "repository_id": analysis.repository.repository_id,
                "graph_name": graph_name,
                "head_commit": analysis.repository.head_commit or "",
                "is_shallow": analysis.repository.is_shallow,
                "is_detached": analysis.repository.is_detached,
                "now": now,
            },
        )
        graph = self.client.select_graph(graph_name)
        try:
            self._ensure_snapshot_indexes(graph)
            self._write_snapshot(analysis)
            self._publish_history(analysis)
            self._publish_dependencies(graph, analysis)
            self._verify_snapshot_graph(graph, analysis)
            ready_at = time.time()
            graph.query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "SET s.status = 'READY', s.ready_at = $ready_at, s.updated_at = $ready_at, "
                "s.job_status = 'COMPLETED'",
                {"snapshot_id": analysis.snapshot_id, "ready_at": ready_at},
            )
            metadata.query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "SET s.status = 'READY', s.ready_at = $ready_at, "
                "s.updated_at = $ready_at, s.terminal_at = NULL, "
                "s.job_status = 'COMPLETED' WITH s "
                "OPTIONAL MATCH (j:IngestionJob {job_id: $job_id}) "
                "SET j.status = CASE WHEN $job_id IS NULL THEN j.status ELSE 'COMPLETED' END, "
                "j.error = CASE WHEN $job_id IS NULL THEN j.error ELSE NULL END",
                {"snapshot_id": analysis.snapshot_id, "job_id": job_id, "ready_at": ready_at},
            )
            analysis.status = SnapshotStatus.READY
            return graph_name
        except Exception:
            try:
                graph.query(
                    "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                    "SET s.status = CASE WHEN s.status = 'READY' THEN 'READY' ELSE 'FAILED' END, "
                    "s.terminal_at = $now, s.updated_at = $now",
                    {"snapshot_id": analysis.snapshot_id, "now": time.time()},
                )
            except Exception:
                pass
            try:
                metadata.query(
                    "MERGE (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                    "SET s.repository_id = $repository_id, s.graph_name = $graph_name, "
                    "s.status = CASE WHEN s.status = 'READY' THEN 'READY' ELSE 'FAILED' END, "
                    "s.terminal_at = $now, s.updated_at = $now",
                    {
                        "snapshot_id": analysis.snapshot_id,
                        "repository_id": analysis.repository.repository_id,
                        "graph_name": graph_name,
                        "now": time.time(),
                    },
                )
            except Exception:
                pass
            raise

    def _publish_history(self, analysis: SnapshotAnalysis) -> None:
        graph = self.metadata_graph()
        graph.query(
            "MERGE (r:Repository {repository_id: $repository_id}) "
            "SET r.root = $root, r.remote = $remote",
            {
                "repository_id": analysis.repository.repository_id,
                "root": analysis.repository.root,
                "remote": analysis.repository.remote,
            },
        )
        for developer in analysis.developers:
            graph.query(
                "MERGE (d:Developer {developer_id: $developer_id}) "
                "SET d.name = $name, d.email = $email",
                {
                    "developer_id": developer.developer_id,
                    "name": developer.name,
                    "email": developer.email,
                },
            )
        for commit in analysis.commits:
            graph.query(
                "MERGE (c:Commit {commit_id: $commit_id}) SET "
                "c.commit_sha = $commit_sha, c.repository_id = $repository_id, "
                "c.parent_commit_ids = $parents, c.parent_commit_shas = $parent_shas, "
                "c.committer_id = $committer_id, c.authored_at = $authored_at, "
                "c.committed_at = $committed_at, c.message = $message "
                "WITH c MATCH (d:Developer {developer_id: $author_id}) "
                "MERGE (d)-[:AUTHORED {edge_id: $authored_edge_id}]->(c)",
                {
                    "commit_id": commit.commit_id,
                    "commit_sha": commit.commit_sha,
                    "repository_id": analysis.repository.repository_id,
                    "parents": commit.parent_commit_ids,
                    "parent_shas": commit.parent_commit_shas,
                    "committer_id": commit.committer_id,
                    "authored_at": commit.authored_at,
                    "committed_at": commit.committed_at,
                    "message": commit.message,
                    "author_id": commit.author_id,
                    "authored_edge_id": stable_id(
                        "AUTHORED", commit.author_id, commit.commit_id, prefix="edge_"
                    ),
                },
            )
        for change in analysis.changed_paths:
            graph.query(
                "MERGE (p:ChangedPath {change_id: $change_id}) SET "
                "p.commit_id = $commit_id, p.parent_commit_sha = $parent_commit_sha, "
                "p.parent_index = $parent_index, p.status = $status, "
                "p.old_path = $old_path, p.new_path = $new_path, "
                "p.rename_similarity = $similarity "
                "WITH p MATCH (c:Commit {commit_id: $commit_id}) "
                "MERGE (c)-[:HAS_CHANGE {edge_id: $edge_id}]->(p)",
                {
                    "change_id": change.change_id,
                    "commit_id": change.commit_id,
                    "parent_commit_sha": change.parent_commit_sha,
                    "parent_index": change.parent_index,
                    "status": change.status.value,
                    "old_path": change.old_path,
                    "new_path": change.new_path,
                    "similarity": change.similarity,
                    "edge_id": stable_id(
                        "HAS_CHANGE", change.commit_id, change.change_id, prefix="edge_"
                    ),
                },
            )

    def _publish_dependencies(self, graph: Any, analysis: SnapshotAnalysis) -> None:
        for component in analysis.dependency_metadata.components:
            graph.query(
                "MERGE (c:ProjectComponent {component_id: $component_id}) SET "
                "c.snapshot_id = $snapshot_id, c.name = $name, c.root_path = $root_path, "
                "c.manifest_files = $manifest_files, c.source_paths = $source_paths "
                "WITH c MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "MERGE (s)-[:HAS_COMPONENT {edge_id: $edge_id}]->(c)",
                {
                    "component_id": component.component_id,
                    "snapshot_id": analysis.snapshot_id,
                    "name": component.name,
                    "root_path": component.root_path,
                    "manifest_files": component.manifest_files,
                    "source_paths": component.source_paths,
                    "edge_id": stable_id(
                        "HAS_COMPONENT",
                        analysis.snapshot_id,
                        component.component_id,
                        prefix="edge_",
                    ),
                },
            )
        for requirement in analysis.dependency_metadata.requirements:
            graph.query(
                "MERGE (d:PackageDeclaration {requirement_id: $requirement_id}) SET "
                "d.snapshot_id = $snapshot_id, d.name = $name, "
                "d.normalized_name = $normalized_name, d.raw_requirement = $raw_requirement, "
                "d.specifier = $specifier, d.marker = $marker, d.extras = $extras, "
                "d.source_file = $source_file, d.optional_group = $optional_group, "
                "d.component_id = $component_id, d.supported = $supported, "
                "d.diagnostic = $diagnostic",
                {
                    "requirement_id": requirement.requirement_id,
                    "snapshot_id": analysis.snapshot_id,
                    "name": requirement.name,
                    "normalized_name": requirement.normalized_name,
                    "raw_requirement": requirement.raw_requirement,
                    "specifier": requirement.specifier,
                    "marker": requirement.marker,
                    "extras": requirement.extras,
                    "source_file": requirement.source_file,
                    "optional_group": requirement.optional_group,
                    "component_id": requirement.component_id,
                    "supported": requirement.supported,
                    "diagnostic": requirement.diagnostic,
                },
            )
            if requirement.component_id:
                graph.query(
                    "MATCH (c:ProjectComponent {component_id: $component_id}), "
                    "(d:PackageDeclaration {requirement_id: $requirement_id}) "
                    "MERGE (c)-[:DECLARES_DEPENDENCY {edge_id: $edge_id}]->(d)",
                    {
                        "component_id": requirement.component_id,
                        "requirement_id": requirement.requirement_id,
                        "edge_id": stable_id(
                            "DECLARES_DEPENDENCY",
                            requirement.component_id,
                            requirement.requirement_id,
                            prefix="edge_",
                        ),
                    },
                )
        for distribution in analysis.dependency_metadata.resolved_distributions:
            graph.query(
                "MERGE (d:PackageDistribution {distribution_id: $distribution_id}) SET "
                "d.snapshot_id = $snapshot_id, d.distribution_name = $distribution_name, "
                "d.version = $version, d.import_names = $import_names, "
                "d.evidence_source = $evidence_source "
                "WITH d MATCH (p:PackageDeclaration {requirement_id: $requirement_id}) "
                "MERGE (p)-[:RESOLVES_TO {edge_id: $edge_id}]->(d)",
                {
                    "distribution_id": distribution.distribution_id,
                    "snapshot_id": analysis.snapshot_id,
                    "distribution_name": distribution.distribution_name,
                    "version": distribution.version,
                    "import_names": distribution.import_names,
                    "evidence_source": distribution.evidence_source,
                    "requirement_id": distribution.requirement_id,
                    "edge_id": stable_id(
                        "RESOLVES_TO",
                        distribution.requirement_id,
                        distribution.distribution_id,
                        prefix="edge_",
                    ),
                },
            )

    def set_snapshot_pinned(self, snapshot_id: str, pinned: bool = True) -> None:
        """Set the explicit/manual retention pin for a snapshot."""
        with snapshot_lifecycle_lock():
            result = self.metadata_graph().query(
                "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                "SET s.manually_pinned = $pinned, s.updated_at = $now "
                "RETURN s.snapshot_id",
                {"snapshot_id": snapshot_id, "pinned": pinned, "now": time.time()},
            )
            if not result.result_set:
                raise KeyError("Snapshot is not registered in FalkorDB")

    def garbage_collect_snapshots(self, now: float | None = None) -> list[str]:
        """Retain each repository's latest READY snapshots and collect eligible graphs."""
        with snapshot_lifecycle_lock():
            current_time = time.time() if now is None else now
            metadata = self.metadata_graph()
            rows = metadata.query(
                "MATCH (s:RepositorySnapshot) "
                "RETURN s.snapshot_id, s.repository_id, s.graph_name, s.status, "
                "s.ready_at, s.terminal_at, s.updated_at, s.manually_pinned "
                "ORDER BY s.repository_id, s.snapshot_id"
            ).result_set
            snapshots: list[dict[str, Any]] = [
                {
                    "snapshot_id": str(row[0]),
                    "repository_id": str(row[1] or ""),
                    "graph_name": str(row[2] or ""),
                    "status": str(row[3] or ""),
                    "ready_at": float(row[4] or 0),
                    "terminal_at": float(row[5]) if row[5] is not None else None,
                    "updated_at": float(row[6]) if row[6] is not None else None,
                    "pinned": bool(row[7]),
                }
                for row in rows
            ]
            pin_rows = metadata.query(
                "MATCH (pin)-[:PINS]->(s:RepositorySnapshot) RETURN DISTINCT s.snapshot_id"
            ).result_set
            pinned_ids = {str(row[0]) for row in pin_rows}
            active_rows = metadata.query(
                "MATCH (j:IngestionJob) WHERE j.status IN "
                "['QUEUED', 'MATERIALIZING', 'PARSING', 'SEMANTIC_INDEXING', "
                "'VALIDATING', 'PUBLISHING'] RETURN DISTINCT j.snapshot_id"
            ).result_set
            active_ids = active_snapshot_ids() | {str(row[0]) for row in active_rows}

            ready_by_repository: dict[str, list[dict[str, Any]]] = {}
            for snapshot in snapshots:
                if snapshot["status"] == "READY":
                    ready_by_repository.setdefault(snapshot["repository_id"], []).append(snapshot)
            retain_ids: set[str] = set()
            for repository_snapshots in ready_by_repository.values():
                newest = sorted(
                    repository_snapshots,
                    key=lambda item: (item["ready_at"], item["snapshot_id"]),
                    reverse=True,
                )[:5]
                retain_ids.update(item["snapshot_id"] for item in newest)

            candidates: list[tuple[dict[str, Any], str]] = []
            grace = self._snapshot_gc_grace_seconds()
            for snapshot in snapshots:
                snapshot_id = snapshot["snapshot_id"]
                status = snapshot["status"]
                if snapshot_id in active_ids or snapshot_id in pinned_ids or snapshot["pinned"]:
                    continue
                if status == "READY":
                    if snapshot_id not in retain_ids:
                        candidates.append((snapshot, status))
                    continue
                if status == "GC_PENDING":
                    candidates.append((snapshot, status))
                    continue
                if status not in {"FAILED", "CANCELLED"}:
                    continue
                terminal_at = snapshot["terminal_at"]
                if terminal_at is None:
                    terminal_at = snapshot["updated_at"]
                if terminal_at is None:
                    metadata.query(
                        "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                        "WHERE s.status IN ['FAILED', 'CANCELLED'] "
                        "SET s.terminal_at = $now, s.updated_at = coalesce(s.updated_at, $now)",
                        {"snapshot_id": snapshot_id, "now": current_time},
                    )
                    continue
                if current_time - terminal_at >= grace:
                    candidates.append((snapshot, status))

            deleted: list[str] = []
            graph_names = set(self.client.list_graphs())
            for snapshot, expected_status in sorted(
                candidates,
                key=lambda pair: (pair[0]["repository_id"], pair[0]["snapshot_id"]),
            ):
                snapshot_id = snapshot["snapshot_id"]
                marked = metadata.query(
                    "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                    "WHERE s.status = $expected_status "
                    "SET s.status = 'GC_PENDING', s.gc_marked_at = $now "
                    "RETURN s.graph_name",
                    {
                        "snapshot_id": snapshot_id,
                        "expected_status": expected_status,
                        "now": current_time,
                    },
                )
                if not marked.result_set:
                    continue
                graph_name = str(marked.result_set[0][0] or snapshot["graph_name"])
                if graph_name and graph_name in graph_names:
                    self.client.select_graph(graph_name).delete()
                    graph_names.discard(graph_name)
                if re.fullmatch(r"snap_[0-9a-f]{64}", snapshot_id):
                    snapshot_cache = (
                        self.settings.snapshot_cache_dir.expanduser().resolve() / snapshot_id
                    )
                    if snapshot_cache.is_dir() and not snapshot_cache.is_symlink():
                        shutil.rmtree(snapshot_cache)
                metadata.query(
                    "MATCH (s:RepositorySnapshot {snapshot_id: $snapshot_id}) "
                    "WHERE s.status = 'GC_PENDING' DETACH DELETE s",
                    {"snapshot_id": snapshot_id},
                )
                deleted.append(snapshot_id)
            return deleted

    def _snapshot_gc_grace_seconds(self) -> int:
        return int(self.settings.failed_snapshot_gc_grace_seconds)

    def update_provenance_replay(self, snapshot_id: str, provenance_id: str, state: Any) -> None:
        graph, _ = self.snapshot_graph(snapshot_id)
        graph.query(
            "MATCH (p:ProvenanceRecord {provenance_id: $provenance_id}) "
            "SET p.replay_state = $replay_state, p.review_state = $review_state",
            {
                "provenance_id": provenance_id,
                "replay_state": state.replay_state.value,
                "review_state": state.review_state.value,
            },
        )

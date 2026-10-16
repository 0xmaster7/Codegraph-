from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from .claims import (
    ClaimModality,
    ClaimPredicate,
    ClaimStatus,
    ClaimType,
    ClaimValidator,
    DraftAnswer,
    DraftClaim,
    ValidatedAnswer,
)
from .fusion import EvidenceType
from .graph_tools import DeterministicGraphTools
from .ids import stable_id
from .orchestrator import QueryResponse, ToolName

MAX_INVESTIGATION_CLAIMS = 100


class EvidencePathNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_id: str
    label: str
    kind: str


class EvidencePathStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    target_id: str
    relation: str
    modality: str
    edge_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    provenance_ids: tuple[str, ...]


class UnresolvedBarrier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str
    expression: str
    file_path: str
    start_line: int | None = None
    resolution: str


class RuntimeObservationCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    test_definition_id: str
    test_invocation_id: str | None
    run_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    observation_kinds: tuple[str, ...]
    evidence_id: str | None
    call_observations: tuple[RuntimeCallEvidence, ...] = ()


class RuntimeCallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    runtime_materialization_id: str | None
    runtime_environment_id: str | None
    test_invocation_id: str | None
    run_id: str | None
    observation_id: str
    source_entity_id: str
    target_entity_id: str
    site_id: str | None
    evidence_id: str


class EvidencePath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    snapshot_id: str
    nodes: tuple[EvidencePathNode, ...]
    steps: tuple[EvidencePathStep, ...]
    hop_count: int
    unresolved_barriers: tuple[UnresolvedBarrier, ...] = ()
    truncated: bool = False


class InvestigationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str | None = None
    repository_id: str
    snapshot_id: str
    query: QueryResponse
    answer: ValidatedAnswer
    evidence_paths: tuple[EvidencePath, ...]
    evidence_paths_truncated: bool
    unresolved_barriers: tuple[UnresolvedBarrier, ...]
    unresolved_barriers_truncated: bool
    runtime_observations: tuple[RuntimeObservationCard, ...]
    runtime_observations_truncated: bool


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _walk(child)


def _claim_id(snapshot_id: str, kind: str, subject: str, object_id: str, *evidence_key: str) -> str:
    digest = stable_id(snapshot_id, kind, subject, object_id, evidence_key, prefix="")
    return f"claim-{digest[:48]}"


def build_candidate_draft(query: QueryResponse) -> DraftAnswer:
    """Build bounded claim candidates only from deterministic M17 results.

    The resulting draft is still untrusted input to M19's validator. No prose or
    relationship is inferred from a question, passage, or ranking score.
    """
    snapshot_id = query.snapshot_id
    if query.evidence_pool.snapshot_id != snapshot_id:
        raise ValueError("Investigation query and evidence pool snapshots differ")
    evidence_ids = {item.item.evidence_id for item in query.evidence_pool.items}
    candidates: dict[str, DraftClaim] = {}
    contexts_by_site = {
        str(record["site_id"]): str(record["context_id"])
        for execution in query.tool_executions
        for record in execution.result.results
        if record.get("site_id") and record.get("context_id")
    }

    def add(claim: DraftClaim) -> None:
        if len(candidates) < MAX_INVESTIGATION_CLAIMS:
            candidates.setdefault(claim.claim_id, claim)

    for execution in query.tool_executions:
        if execution.result.snapshot_id != snapshot_id:
            raise ValueError("Investigation tool output belongs to another snapshot")
        if execution.result.tool in {ToolName.FIND_CALLERS.value, ToolName.FIND_CALLEES.value}:
            for record in execution.result.results:
                if (
                    execution.result.tool == ToolName.FIND_CALLEES.value
                    and record.get("resolution") != "EXACT"
                ):
                    continue
                # FIND_CALLERS returns only exact SCIP bindings by contract and
                # does not redundantly serialize a resolution field.
                if execution.result.tool == ToolName.FIND_CALLERS.value and (
                    record.get("edge_id") is None or record.get("resolution") not in {None, "EXACT"}
                ):
                    continue
                evidence = record.get("evidence")
                if not isinstance(evidence, Mapping):
                    continue
                evidence_id = evidence.get("evidence_id")
                if (
                    evidence.get("type") != EvidenceType.STATIC_BINDING.value
                    or evidence_id not in evidence_ids
                ):
                    continue
                if execution.result.tool == ToolName.FIND_CALLERS.value:
                    subject = (record.get("caller") or {}).get("id")
                    object_id = record.get("target_id")
                else:
                    request_site = execution.request.site_id if execution.request else None
                    subject = contexts_by_site.get(str(request_site))
                    object_id = record.get("target_id")
                if not isinstance(subject, str) or not isinstance(object_id, str):
                    continue
                add(
                    DraftClaim(
                        claim_id=_claim_id(
                            snapshot_id, "direct-call", subject, object_id, str(evidence_id)
                        ),
                        claim_type=ClaimType.DIRECT_CALL,
                        text="Candidate derived from an exact M17 call binding.",
                        subject_id=subject,
                        predicate=ClaimPredicate.DIRECT_CALL,
                        object_id=object_id,
                        modality=ClaimModality.EXACT,
                        evidence_ids=(str(evidence_id),),
                    )
                )
        elif execution.result.tool == ToolName.FIND_PATHS.value:
            for path in execution.result.results:
                steps = path.get("steps")
                if not isinstance(steps, list) or not steps:
                    continue
                if not all(
                    isinstance(step, Mapping) and step.get("verified_graph_path") is True
                    for step in steps
                ):
                    continue
                path_evidence = sorted(
                    {
                        str(value["evidence_id"])
                        for step in steps
                        for value in _walk(step)
                        if isinstance(value.get("evidence_id"), str)
                    }
                )
                if not path_evidence or not set(path_evidence).issubset(evidence_ids):
                    continue
                source_id, target_id = path.get("source_id"), path.get("target_id")
                if not isinstance(source_id, str) or not isinstance(target_id, str):
                    continue
                uncertain = int(path.get("uncertainty_count") or 0) > 0
                derived = any(step.get("relation") in {"INHERITS", "OVERRIDES"} for step in steps)
                modality = (
                    ClaimModality.MAY
                    if uncertain
                    else ClaimModality.DERIVED
                    if derived
                    else ClaimModality.EXACT
                )
                predicate = ClaimPredicate.MAY_REACH if uncertain else ClaimPredicate.REACHES
                add(
                    DraftClaim(
                        claim_id=_claim_id(
                            snapshot_id, "path", source_id, target_id, *path_evidence
                        ),
                        claim_type=ClaimType.STRUCTURAL_PATH,
                        text="Candidate derived from a returned M17 path.",
                        subject_id=source_id,
                        predicate=predicate,
                        object_id=target_id,
                        modality=modality,
                        evidence_ids=tuple(path_evidence),
                    )
                )

    passage_items = sorted(
        (
            item.item
            for item in query.evidence_pool.items
            if item.item.type in {EvidenceType.LEXICAL_PASSAGE, EvidenceType.SEMANTIC_PASSAGE}
            and item.item.text
            and (item.item.source_node_id or item.item.chunk_id)
        ),
        key=lambda item: item.evidence_id,
    )
    for item in passage_items:
        if item.evidence_id not in evidence_ids:
            continue
        source_id = item.source_node_id or item.chunk_id
        if not source_id or not item.chunk_id:
            continue
        modality = (
            ClaimModality.SEMANTIC
            if item.type == EvidenceType.SEMANTIC_PASSAGE
            else ClaimModality.LEXICAL
        )
        add(
            DraftClaim(
                claim_id=_claim_id(snapshot_id, "passage", source_id, item.chunk_id),
                claim_type=ClaimType.PASSAGE_STATEMENT,
                text="Candidate for a retrieved passage; not a structural fact.",
                subject_id=source_id,
                predicate=ClaimPredicate.DOCUMENTATION_STATES,
                object_id=item.chunk_id,
                modality=modality,
                evidence_ids=(item.evidence_id,),
            )
        )
    ordered = tuple(
        sorted(
            candidates.values(),
            key=lambda item: (
                item.claim_type == ClaimType.PASSAGE_STATEMENT,
                item.claim_type.value,
                item.subject_id,
                item.object_id,
                item.claim_id,
            ),
        )
    )
    return DraftAnswer(snapshot_id=snapshot_id, claims=ordered)


def _barriers(
    query: QueryResponse, tools: DeterministicGraphTools
) -> tuple[UnresolvedBarrier, ...]:
    records = {
        str(item.get("site_id")): item
        for execution in query.tool_executions
        for item in execution.result.unresolved_barriers
        if item.get("site_id")
    }
    sites = {item.site_id: item for item in tools.analysis.sites}
    return tuple(
        UnresolvedBarrier(
            site_id=str(item["site_id"]),
            expression=str(item.get("expression") or ""),
            file_path=str(item.get("file_path") or ""),
            start_line=(
                sites[str(item["site_id"])].span.start_line
                if str(item["site_id"]) in sites
                else None
            ),
            resolution=str(item.get("resolution") or "UNRESOLVED"),
        )
        for _, item in sorted(records.items())[:MAX_INVESTIGATION_CLAIMS]
    )


def build_investigation_response(
    query: QueryResponse,
    tools: DeterministicGraphTools,
) -> InvestigationResponse:
    draft = build_candidate_draft(query)
    answer = ClaimValidator(tools).validate(
        draft,
        evidence_pool=query.evidence_pool,
        executions=query.tool_executions,
        diagnostics=query.diagnostics,
    )
    paths: list[EvidencePath] = []
    node_labels = {
        item.entity_id: (item.qualified_name, item.kind) for item in tools.analysis.entities
    }
    node_labels.update(
        (item.external_symbol_id, (item.semantic_symbol, item.external_kind))
        for item in tools.analysis.external_symbols
    )
    barriers = _barriers(query, tools)
    for execution in query.tool_executions:
        if execution.result.tool != ToolName.FIND_PATHS.value:
            continue
        for path in execution.result.results:
            steps = path.get("steps")
            if not isinstance(steps, list) or not steps:
                continue
            evidence_ids = {
                str(value["evidence_id"])
                for step in steps
                for value in _walk(step)
                if isinstance(value.get("evidence_id"), str)
            }
            claim = next(
                (
                    item
                    for item in answer.claims
                    if item.claim_type == ClaimType.STRUCTURAL_PATH
                    and item.subject_id == path.get("source_id")
                    and item.object_id == path.get("target_id")
                    and set(item.evidence_ids) == evidence_ids
                    and item.status in {ClaimStatus.SUPPORTED, ClaimStatus.QUALIFIED}
                ),
                None,
            )
            if claim is None:
                continue
            node_ids = path.get("node_ids")
            if not isinstance(node_ids, list) or len(node_ids) != len(steps) + 1:
                continue
            nodes = tuple(
                EvidencePathNode(
                    canonical_id=str(node_id),
                    label=node_labels.get(str(node_id), (str(node_id), "UNKNOWN"))[0],
                    kind=node_labels.get(str(node_id), (str(node_id), "UNKNOWN"))[1],
                )
                for node_id in node_ids
            )
            path_steps = []
            for step in steps:
                edge_evidence = tuple(
                    sorted(
                        value["evidence_id"]
                        for value in _walk(step)
                        if isinstance(value.get("evidence_id"), str)
                    )
                )
                relation = str(step.get("relation") or "UNKNOWN")
                modality = (
                    "MAY_DISPATCH"
                    if relation in {"MAY_DISPATCH_TO", "CONSERVATIVE_DISPATCH"}
                    else "DERIVED"
                    if relation in {"INHERITS", "OVERRIDES"}
                    else "EXACT"
                )
                path_steps.append(
                    EvidencePathStep(
                        source_id=str(step.get("source_id") or ""),
                        target_id=str(step.get("target_id") or ""),
                        relation=relation,
                        modality=modality,
                        edge_ids=tuple(sorted(str(value) for value in step.get("edge_ids", ()))),
                        evidence_ids=edge_evidence,
                        provenance_ids=tuple(
                            sorted(str(value) for value in step.get("provenance_ids", ()))
                        ),
                    )
                )
            paths.append(
                EvidencePath(
                    claim_id=claim.claim_id,
                    snapshot_id=query.snapshot_id,
                    nodes=nodes,
                    steps=tuple(path_steps),
                    hop_count=int(path.get("hop_count") or len(path_steps)),
                    unresolved_barriers=barriers,
                    truncated=execution.result.truncated,
                )
            )
    paths.sort(
        key=lambda item: (
            item.hop_count,
            tuple(node.canonical_id for node in item.nodes),
            item.claim_id,
        )
    )
    runtime_cards = []
    for execution in query.tool_executions:
        if execution.result.tool != ToolName.FIND_OBSERVED_TESTS.value:
            continue
        for record in execution.result.results:
            evidence = record.get("evidence")
            runtime_cards.append(
                RuntimeObservationCard(
                    snapshot_id=query.snapshot_id,
                    test_definition_id=str(record.get("test_definition_id") or ""),
                    test_invocation_id=(
                        str(record["test_invocation_id"])
                        if record.get("test_invocation_id")
                        else None
                    ),
                    run_ids=tuple(sorted(str(value) for value in record.get("run_ids", ()))),
                    entity_ids=tuple(sorted(str(value) for value in record.get("entity_ids", ()))),
                    observation_kinds=tuple(
                        sorted(str(value) for value in record.get("observation_kinds", ()))
                    ),
                    evidence_id=(
                        str(evidence.get("evidence_id"))
                        if isinstance(evidence, Mapping) and evidence.get("evidence_id")
                        else None
                    ),
                    call_observations=tuple(
                        RuntimeCallEvidence(
                            snapshot_id=str(call.get("snapshot_id") or query.snapshot_id),
                            runtime_materialization_id=(
                                str(call["runtime_materialization_id"])
                                if call.get("runtime_materialization_id")
                                else None
                            ),
                            runtime_environment_id=(
                                str(call["runtime_environment_id"])
                                if call.get("runtime_environment_id")
                                else None
                            ),
                            test_invocation_id=(
                                str(call["test_invocation_id"])
                                if call.get("test_invocation_id")
                                else None
                            ),
                            run_id=str(call["run_id"]) if call.get("run_id") else None,
                            observation_id=str(call.get("observation_id") or ""),
                            source_entity_id=str(call.get("source_entity_id") or ""),
                            target_entity_id=str(call.get("target_entity_id") or ""),
                            site_id=str(call["site_id"]) if call.get("site_id") else None,
                            evidence_id=str((call.get("evidence") or {}).get("evidence_id") or ""),
                        )
                        for call in record.get("call_observations", ())
                        if isinstance(call, Mapping)
                    ),
                )
            )
    runtime_cards.sort(
        key=lambda item: (item.test_definition_id, item.test_invocation_id or "", item.run_ids)
    )
    path_limit = MAX_INVESTIGATION_CLAIMS
    runtime_limit = MAX_INVESTIGATION_CLAIMS
    barrier_count = len(
        {
            str(item.get("site_id"))
            for execution in query.tool_executions
            for item in execution.result.unresolved_barriers
            if item.get("site_id")
        }
    )
    path_count = sum(
        len(execution.result.results)
        for execution in query.tool_executions
        if execution.result.tool == ToolName.FIND_PATHS.value
    )
    runtime_count = sum(
        len(execution.result.results)
        for execution in query.tool_executions
        if execution.result.tool == ToolName.FIND_OBSERVED_TESTS.value
    )
    return InvestigationResponse(
        repository_id=tools.analysis.repository.repository_id,
        snapshot_id=query.snapshot_id,
        query=query,
        answer=answer,
        evidence_paths=tuple(paths[:path_limit]),
        evidence_paths_truncated=path_count > path_limit,
        unresolved_barriers=barriers,
        unresolved_barriers_truncated=barrier_count > MAX_INVESTIGATION_CLAIMS,
        runtime_observations=tuple(runtime_cards[:runtime_limit]),
        runtime_observations_truncated=runtime_count > runtime_limit,
    )

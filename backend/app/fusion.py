from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .ids import stable_id
from .models import ReplayOutcome, ReviewState

RRF_K = 60


class EvidenceType(StrEnum):
    STATIC_STRUCTURE = "STATIC_STRUCTURE"
    STATIC_BINDING = "STATIC_BINDING"
    STATIC_DERIVATION = "STATIC_DERIVATION"
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"
    GIT_HISTORY = "GIT_HISTORY"
    DEPENDENCY_METADATA = "DEPENDENCY_METADATA"
    LEXICAL_PASSAGE = "LEXICAL_PASSAGE"
    SEMANTIC_PASSAGE = "SEMANTIC_PASSAGE"
    UNRESOLVED_DIAGNOSTIC = "UNRESOLVED_DIAGNOSTIC"
    USER_CONTEXT = "USER_CONTEXT"


class RetrievalChannel(StrEnum):
    LEXICAL = "LEXICAL"
    VECTOR = "VECTOR"


class RetrievalSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel: RetrievalChannel
    rank: int = Field(ge=1)
    rrf_contribution: float = Field(gt=0)


class EvidenceItem(BaseModel):
    """One normalized evidence record using the canonical plan taxonomy."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    type: EvidenceType
    snapshot_id: str
    source_record_id: str
    display_label: str
    provenance_pointer: str
    chunk_id: str | None = None
    chunk_kind: str | None = None
    source_node_id: str | None = None
    file_path: str | None = None
    content_hash: str | None = None
    text: str | None = None
    retrieval_signals: tuple[RetrievalSignal, ...] = ()
    fused_score: float = 0.0
    rank: int | None = None
    review_state: ReviewState = ReviewState.NONE
    replay_outcome: ReplayOutcome | None = None


class RankedPassage(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    snapshot_id: str
    source_node_id: str
    file_path: str
    text: str
    content_hash: str
    chunk_kind: str
    source_start_byte: int = 0
    source_end_byte: int = 0


class SourceAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_node_id: str
    fused_score: float
    chunk_ids: tuple[str, ...]


class ConsistencyAssertion(BaseModel):
    """Comparable facts needed by the explicit M16 conflict rules."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    type: EvidenceType
    snapshot_id: str
    occurrence_id: str | None = None
    resolver: str | None = None
    resolved_target_id: str | None = None
    expected_source_hash: str | None = None
    observed_source_hash: str | None = None
    static_candidate_ids: tuple[str, ...] = ()
    observed_runtime_target_id: str | None = None


class ConsistencyFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_state: ReviewState
    subject_id: str
    evidence_ids: tuple[str, ...]
    reason: str


class ConsistencyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    findings: tuple[ConsistencyFinding, ...] = ()

    @property
    def requires_review(self) -> bool:
        return bool(self.findings)


class EvidencePool(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    items: tuple[EvidenceItem, ...]
    source_aggregates: tuple[SourceAggregate, ...]
    mandatory_seed_entity_ids: tuple[str, ...] = ()
    consistency: ConsistencyReport


class EvidenceScopeError(ValueError):
    pass


def normalize_graph_evidence(
    *,
    evidence_type: EvidenceType,
    snapshot_id: str,
    source_record_id: str,
    display_label: str,
    provenance_pointer: str,
    review_state: ReviewState = ReviewState.NONE,
    replay_outcome: ReplayOutcome | None = None,
) -> EvidenceItem:
    """Normalize deterministic graph, runtime, metadata, or diagnostic evidence."""
    return EvidenceItem(
        evidence_id=stable_id(
            snapshot_id, evidence_type.value, source_record_id, prefix="evidence_"
        ),
        type=evidence_type,
        snapshot_id=snapshot_id,
        source_record_id=source_record_id,
        display_label=display_label,
        provenance_pointer=provenance_pointer,
        review_state=review_state,
        replay_outcome=replay_outcome,
    )


def assess_consistency(
    assertions: Sequence[ConsistencyAssertion], *, snapshot_id: str
) -> ConsistencyReport:
    """Apply only plan-defined conflicts; unrelated evidence dimensions stay separate."""
    if any(item.snapshot_id != snapshot_id for item in assertions):
        raise EvidenceScopeError("Evidence from distinct snapshots cannot share a consistency pool")

    findings: list[ConsistencyFinding] = []
    resolver_groups: dict[str, list[ConsistencyAssertion]] = defaultdict(list)
    for item in assertions:
        if item.occurrence_id and item.resolver and item.resolved_target_id:
            resolver_groups[item.occurrence_id].append(item)

        if (
            item.expected_source_hash is not None
            and item.observed_source_hash is not None
            and item.expected_source_hash != item.observed_source_hash
        ):
            findings.append(
                ConsistencyFinding(
                    review_state=ReviewState.REPLAY_FAILED,
                    subject_id=item.occurrence_id or item.evidence_id,
                    evidence_ids=(item.evidence_id,),
                    reason="Observed source content hash differs from the snapshot evidence hash.",
                )
            )

        if (
            item.type == EvidenceType.RUNTIME_OBSERVATION
            and item.observed_runtime_target_id
            and item.static_candidate_ids
            and item.observed_runtime_target_id not in item.static_candidate_ids
        ):
            findings.append(
                ConsistencyFinding(
                    review_state=ReviewState.RUNTIME_MISMATCH,
                    subject_id=item.occurrence_id or item.evidence_id,
                    evidence_ids=(item.evidence_id,),
                    reason=(
                        "Observed runtime target is outside this call site's static candidate set."
                    ),
                )
            )

    for occurrence_id, records in sorted(resolver_groups.items()):
        resolver_targets: dict[str, set[str]] = defaultdict(set)
        for item in records:
            assert item.resolver is not None and item.resolved_target_id is not None
            resolver_targets[item.resolver].add(item.resolved_target_id)
        targets = {target for values in resolver_targets.values() for target in values}
        if len(targets) <= 1:
            continue
        resolver_names = sorted(resolver_targets)
        cross_resolver = (
            any(len(resolver_targets[name]) > 1 for name in resolver_names)
            or len(resolver_names) > 1
        )
        findings.append(
            ConsistencyFinding(
                review_state=(
                    ReviewState.RESOLVER_DISAGREEMENT
                    if cross_resolver
                    else ReviewState.REPLAY_FAILED
                ),
                subject_id=occurrence_id,
                evidence_ids=tuple(sorted(item.evidence_id for item in records)),
                reason=(
                    "Resolvers or replay records disagree on the target for one source occurrence."
                ),
            )
        )

    findings.sort(key=lambda item: (item.subject_id, item.review_state.value, item.evidence_ids))
    return ConsistencyReport(snapshot_id=snapshot_id, findings=tuple(findings))


def fuse_passage_ranks(
    snapshot_id: str,
    lexical_ranked: Sequence[RankedPassage],
    vector_ranked: Sequence[RankedPassage],
    *,
    exact_entity_seed_ids: Sequence[str] = (),
    rrf_k: int = RRF_K,
) -> EvidencePool:
    """Fuse lexical and vector ranks at chunk identity, then aggregate by source node."""
    if not snapshot_id:
        raise ValueError("A snapshot identity is required for passage fusion")
    if rrf_k < 0:
        raise ValueError("RRF k must be nonnegative")
    channels = (
        (RetrievalChannel.LEXICAL, EvidenceType.LEXICAL_PASSAGE, lexical_ranked),
        (RetrievalChannel.VECTOR, EvidenceType.SEMANTIC_PASSAGE, vector_ranked),
    )
    canonical: dict[str, RankedPassage] = {}
    contributions: dict[str, list[RetrievalSignal]] = defaultdict(list)
    inconsistencies: dict[str, set[str]] = defaultdict(set)

    for channel, _, passages in channels:
        seen: set[str] = set()
        first_in_channel: dict[str, RankedPassage] = {}
        rank = 0
        for passage in passages:
            if passage.snapshot_id != snapshot_id:
                raise EvidenceScopeError("Passage belongs to a different snapshot")
            if passage.chunk_id in seen:
                if first_in_channel[passage.chunk_id] != passage:
                    previous = canonical[passage.chunk_id]
                    inconsistencies[passage.chunk_id].update(
                        {previous.source_node_id, passage.source_node_id}
                    )
                continue
            seen.add(passage.chunk_id)
            first_in_channel[passage.chunk_id] = passage
            rank += 1
            previous = canonical.setdefault(passage.chunk_id, passage)
            if previous != passage:
                inconsistencies[passage.chunk_id].update(
                    {previous.source_node_id, passage.source_node_id}
                )
                continue
            contributions[passage.chunk_id].append(
                RetrievalSignal(
                    channel=channel,
                    rank=rank,
                    rrf_contribution=1.0 / (rrf_k + rank),
                )
            )

    invalid_ids = set(inconsistencies)
    reviews = [
        ConsistencyFinding(
            review_state=ReviewState.SCHEMA_WARNING,
            subject_id=chunk_id,
            evidence_ids=(chunk_id,),
            reason="One chunk identity resolved to inconsistent passage provenance or content.",
        )
        for chunk_id in sorted(invalid_ids)
    ]
    items: list[EvidenceItem] = []
    for chunk_id, passage in canonical.items():
        if chunk_id in invalid_ids:
            continue
        signals = tuple(contributions[chunk_id])
        evidence_type = (
            EvidenceType.SEMANTIC_PASSAGE
            if any(signal.channel == RetrievalChannel.VECTOR for signal in signals)
            else EvidenceType.LEXICAL_PASSAGE
        )
        score = sum(signal.rrf_contribution for signal in signals)
        items.append(
            EvidenceItem(
                evidence_id=stable_id(snapshot_id, "passage", chunk_id, prefix="evidence_"),
                type=evidence_type,
                snapshot_id=snapshot_id,
                source_record_id=chunk_id,
                display_label=f"{passage.file_path}:{passage.source_start_byte}-{passage.source_end_byte}",
                provenance_pointer=(
                    f"{snapshot_id}:{passage.file_path}@{passage.source_start_byte}:"
                    f"{passage.source_end_byte}#{passage.content_hash}"
                ),
                chunk_id=chunk_id,
                source_node_id=passage.source_node_id,
                file_path=passage.file_path,
                content_hash=passage.content_hash,
                text=passage.text,
                retrieval_signals=signals,
                fused_score=score,
            )
        )

    items.sort(key=lambda item: (-item.fused_score, item.chunk_id or ""))
    items = [item.model_copy(update={"rank": index}) for index, item in enumerate(items, 1)]

    source_scores: dict[str, float] = defaultdict(float)
    source_chunks: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in items:
        assert item.source_node_id is not None and item.chunk_id is not None
        source_scores[item.source_node_id] += item.fused_score
        source_chunks[item.source_node_id].append(item)
    aggregates = [
        SourceAggregate(
            source_node_id=source_id,
            fused_score=score,
            chunk_ids=tuple(item.chunk_id for item in source_chunks[source_id] if item.chunk_id),
        )
        for source_id, score in source_scores.items()
    ]
    aggregates.sort(key=lambda item: (-item.fused_score, item.source_node_id))
    consistency = ConsistencyReport(snapshot_id=snapshot_id, findings=tuple(reviews))
    return EvidencePool(
        snapshot_id=snapshot_id,
        items=tuple(items),
        source_aggregates=tuple(aggregates),
        mandatory_seed_entity_ids=tuple(sorted(set(exact_entity_seed_ids))),
        consistency=consistency,
    )


def passage_from_graph_record(record: dict[str, object]) -> RankedPassage:
    """Validate a FalkorDB full-text/vector row as passage evidence."""
    return RankedPassage.model_validate(record)

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .claims import ClaimStatus, ValidatedClaim
from .graph import GraphStore, GraphWriteConflict
from .investigation import EvidencePath, InvestigationResponse

SelectionKind = Literal["entity", "site", "edge"]


class InvestigationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SelectionKind
    selected_id: str = Field(min_length=1, max_length=256)


class InvestigationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=4096)
    selected_entities: tuple[InvestigationSelection, ...] = Field(max_length=3)
    asked_at: float = Field(gt=0)


class InvestigationNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    note_id: str = Field(pattern=r"^note_[0-9a-f]{32}$")
    text: str = Field(min_length=1, max_length=4000)
    created_at: float = Field(gt=0)


class InvestigationMemoryRecord(BaseModel):
    """Persistent, snapshot-bound workspace state, separate from graph facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(pattern=r"^investigation_[0-9a-f]{32}$")
    repository_id: str = Field(min_length=1, max_length=160)
    snapshot_id: str = Field(min_length=1, max_length=128)
    created_at: float = Field(gt=0)
    updated_at: float = Field(gt=0)
    record_revision: int = Field(default=0, ge=0)
    questions: tuple[InvestigationQuestion, ...] = Field(max_length=100)
    selected_entities: tuple[InvestigationSelection, ...] = Field(max_length=500)
    validated_claims: tuple[ValidatedClaim, ...] = Field(max_length=500)
    evidence_paths: tuple[EvidencePath, ...] = Field(max_length=500)
    runtime_run_ids: tuple[str, ...] = Field(max_length=500)
    user_notes: tuple[InvestigationNote, ...] = Field(max_length=100)
    latest_result: InvestigationResponse

    @model_validator(mode="after")
    def validate_snapshot_bound_data(self) -> InvestigationMemoryRecord:
        if self.latest_result.snapshot_id != self.snapshot_id:
            raise ValueError("Investigation result belongs to another snapshot")
        if self.latest_result.repository_id != self.repository_id:
            raise ValueError("Investigation result belongs to another repository")
        if self.latest_result.investigation_id != self.investigation_id:
            raise ValueError("Investigation result identity does not match its record")
        if any(path.snapshot_id != self.snapshot_id for path in self.evidence_paths):
            raise ValueError("Investigation evidence path belongs to another snapshot")
        if any(
            claim.status not in {ClaimStatus.SUPPORTED, ClaimStatus.QUALIFIED}
            for claim in self.validated_claims
        ):
            raise ValueError("Only validated claims may be persisted as supported memory")
        return self


class InvestigationMemorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    repository_id: str
    snapshot_id: str
    last_question: str
    updated_at: float
    note_count: int


class InvestigationSnapshotMismatch(ValueError):
    def __init__(self, investigation_id: str, stored_snapshot_id: str, requested_snapshot_id: str):
        self.investigation_id = investigation_id
        self.stored_snapshot_id = stored_snapshot_id
        self.requested_snapshot_id = requested_snapshot_id
        super().__init__("Investigation belongs to a different snapshot")


class InvestigationMemoryService:
    MAX_WRITE_ATTEMPTS = 5

    def __init__(
        self,
        store: GraphStore,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.id_factory = id_factory or (lambda: f"investigation_{uuid4().hex}")

    def list(self, snapshot_id: str, limit: int = 20) -> list[InvestigationMemorySummary]:
        if not 1 <= limit <= 100:
            raise ValueError("Investigation limit must be between 1 and 100")
        records = [
            InvestigationMemoryRecord.model_validate_json(value)
            for value in self.store.list_investigation_memories(snapshot_id, limit)
        ]
        return [
            InvestigationMemorySummary(
                investigation_id=record.investigation_id,
                repository_id=record.repository_id,
                snapshot_id=record.snapshot_id,
                last_question=record.questions[-1].question,
                updated_at=record.updated_at,
                note_count=len(record.user_notes),
            )
            for record in records
            if record.questions
        ]

    def get(
        self, investigation_id: str, *, snapshot_id: str | None = None
    ) -> InvestigationMemoryRecord:
        raw = self.store.get_investigation_memory(investigation_id)
        if raw is None:
            raise LookupError("Investigation not found")
        record = InvestigationMemoryRecord.model_validate_json(raw)
        if snapshot_id is not None and snapshot_id != record.snapshot_id:
            raise InvestigationSnapshotMismatch(investigation_id, record.snapshot_id, snapshot_id)
        return record

    def save_result(
        self,
        response: InvestigationResponse,
        *,
        question: str,
        selected_entities: tuple[InvestigationSelection, ...] = (),
        investigation_id: str | None = None,
    ) -> InvestigationMemoryRecord:
        record_id = investigation_id or self.id_factory()
        response = response.model_copy(update={"investigation_id": record_id})
        for attempt in range(self.MAX_WRITE_ATTEMPTS):
            current_time = self.clock()
            prior: InvestigationMemoryRecord | None = None
            if investigation_id is not None:
                prior = self.get(investigation_id, snapshot_id=response.snapshot_id)
            questions = (() if prior is None else prior.questions) + (
                InvestigationQuestion(
                    question=question,
                    selected_entities=selected_entities,
                    asked_at=current_time,
                ),
            )
            selections = list(() if prior is None else prior.selected_entities)
            for selected in selected_entities:
                if selected not in selections:
                    selections.append(selected)
            claims = {
                claim.claim_id: claim for claim in (() if prior is None else prior.validated_claims)
            }
            claims.update(
                {
                    claim.claim_id: claim
                    for claim in response.answer.claims
                    if claim.status in {ClaimStatus.SUPPORTED, ClaimStatus.QUALIFIED}
                }
            )
            paths = {
                self._path_key(path): path
                for path in (() if prior is None else prior.evidence_paths)
            }
            paths.update({self._path_key(path): path for path in response.evidence_paths})
            runtime_run_ids = set(() if prior is None else prior.runtime_run_ids)
            for observation in response.runtime_observations:
                if observation.snapshot_id != response.snapshot_id:
                    raise ValueError("Runtime observation belongs to another snapshot")
                runtime_run_ids.update(observation.run_ids)
                for call in observation.call_observations:
                    if call.snapshot_id != response.snapshot_id:
                        raise ValueError("Runtime call belongs to another snapshot")
                    if call.run_id:
                        runtime_run_ids.add(call.run_id)
            expected_revision = prior.record_revision if prior else 0
            record = InvestigationMemoryRecord(
                investigation_id=record_id,
                repository_id=response.repository_id,
                snapshot_id=response.snapshot_id,
                created_at=prior.created_at if prior else current_time,
                updated_at=current_time,
                record_revision=expected_revision + 1,
                questions=questions[-100:],
                selected_entities=tuple(selections[-500:]),
                validated_claims=tuple(claims[key] for key in sorted(claims)[-500:]),
                evidence_paths=tuple(paths[key] for key in sorted(paths)[-500:]),
                runtime_run_ids=tuple(sorted(runtime_run_ids)[-500:]),
                user_notes=() if prior is None else prior.user_notes,
                latest_result=response,
            )
            try:
                self.store.save_investigation_memory(
                    record.investigation_id,
                    record.snapshot_id,
                    record.model_dump_json(),
                    record.created_at,
                    record.updated_at,
                    expected_revision=expected_revision,
                )
                return record
            except GraphWriteConflict:
                if attempt + 1 == self.MAX_WRITE_ATTEMPTS:
                    raise
        raise GraphWriteConflict("Investigation update retry limit reached")

    def add_note(
        self, investigation_id: str, *, snapshot_id: str, text: str
    ) -> InvestigationMemoryRecord:
        if not text.strip():
            raise ValueError("Investigation note cannot be empty")
        note = InvestigationNote(note_id=f"note_{uuid4().hex}", text=text, created_at=self.clock())
        for attempt in range(self.MAX_WRITE_ATTEMPTS):
            prior = self.get(investigation_id, snapshot_id=snapshot_id)
            if len(prior.user_notes) >= 100:
                raise ValueError("Investigation note limit reached")
            expected_revision = prior.record_revision
            record = prior.model_copy(
                update={
                    "user_notes": (*prior.user_notes, note),
                    "updated_at": self.clock(),
                    "record_revision": expected_revision + 1,
                }
            )
            try:
                self.store.save_investigation_memory(
                    record.investigation_id,
                    record.snapshot_id,
                    record.model_dump_json(),
                    record.created_at,
                    record.updated_at,
                    expected_revision=expected_revision,
                )
                return record
            except GraphWriteConflict:
                if attempt + 1 == self.MAX_WRITE_ATTEMPTS:
                    raise
        raise GraphWriteConflict("Investigation update retry limit reached")

    @staticmethod
    def _path_key(path: EvidencePath) -> str:
        return f"{path.claim_id}:{','.join(node.canonical_id for node in path.nodes)}"

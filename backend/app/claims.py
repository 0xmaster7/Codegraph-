from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fusion import EvidenceType
from .graph_tools import DeterministicGraphTools
from .orchestrator import OrchestratedEvidence, QueryEvidencePool, QueryToolExecution, ToolName

MAX_DRAFT_CLAIMS = 100
MAX_CLAIM_EVIDENCE_IDS = 32
MAX_DRAFT_LIMITATIONS = 32
CANONICAL_ID_PATTERN = r"^[A-Za-z0-9_./:@+-]{1,256}$"


class ClaimType(StrEnum):
    DIRECT_CALL = "DIRECT_CALL"
    STRUCTURAL_PATH = "STRUCTURAL_PATH"
    OBSERVED_CALL = "OBSERVED_CALL"
    CHANGED_IN_COMMIT = "CHANGED_IN_COMMIT"
    DEPENDENCY_DECLARED = "DEPENDENCY_DECLARED"
    PASSAGE_STATEMENT = "PASSAGE_STATEMENT"


class ClaimModality(StrEnum):
    EXACT = "EXACT"
    DERIVED = "DERIVED"
    MAY = "MAY"
    OBSERVED = "OBSERVED"
    LEXICAL = "LEXICAL"
    SEMANTIC = "SEMANTIC"


class ClaimStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    CONFLICTING = "CONFLICTING"


class ClaimValidationReason(StrEnum):
    SUPPORTED_BY_EVIDENCE = "SUPPORTED_BY_EVIDENCE"
    QUALIFIED_BY_EVIDENCE = "QUALIFIED_BY_EVIDENCE"
    UNSUPPORTED_BY_EVIDENCE = "UNSUPPORTED_BY_EVIDENCE"
    INVALID_EVIDENCE_REFERENCE = "INVALID_EVIDENCE_REFERENCE"
    INVALID_CLAIM_EVIDENCE_MATCH = "INVALID_CLAIM_EVIDENCE_MATCH"
    CROSS_SNAPSHOT_EVIDENCE = "CROSS_SNAPSHOT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    AMBIGUOUS_OR_UNRESOLVED = "AMBIGUOUS_OR_UNRESOLVED"
    MODALITY_MISMATCH = "MODALITY_MISMATCH"
    REPLAY_INTEGRITY_FAILURE = "REPLAY_INTEGRITY_FAILURE"


class ClaimPredicate(StrEnum):
    DIRECT_CALL = "DIRECT_CALL"
    REACHES = "REACHES"
    MAY_REACH = "MAY_REACH"
    OBSERVED_CALL = "OBSERVED_CALL"
    CHANGED_IN_COMMIT = "CHANGED_IN_COMMIT"
    DEPENDENCY_DECLARED = "DEPENDENCY_DECLARED"
    DOCUMENTATION_STATES = "DOCUMENTATION_STATES"


class DraftClaim(BaseModel):
    """Untrusted structured claim proposal. Its text is never rendered directly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    claim_type: ClaimType
    text: str = Field(min_length=1, max_length=2_000)
    subject_id: str = Field(pattern=CANONICAL_ID_PATTERN)
    predicate: ClaimPredicate
    object_id: str = Field(pattern=CANONICAL_ID_PATTERN)
    modality: ClaimModality
    evidence_ids: tuple[Annotated[str, Field(pattern=CANONICAL_ID_PATTERN)], ...] = Field(
        min_length=1, max_length=MAX_CLAIM_EVIDENCE_IDS
    )

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> DraftClaim:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("A claim cannot repeat an evidence ID")
        return self


class DraftAnswer(BaseModel):
    """Bounded draft received from an optional answer generator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=128)
    claims: tuple[DraftClaim, ...] = Field(max_length=MAX_DRAFT_CLAIMS)
    limitations: tuple[Annotated[str, Field(max_length=1_000)], ...] = Field(
        default=(), max_length=MAX_DRAFT_LIMITATIONS
    )

    @model_validator(mode="after")
    def validate_unique_claim_ids(self) -> DraftAnswer:
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("Draft claim IDs must be unique")
        return self


class ValidatedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    claim_type: ClaimType
    subject_id: str
    predicate: ClaimPredicate
    object_id: str
    modality: ClaimModality
    evidence_ids: tuple[str, ...]
    status: ClaimStatus
    reason: ClaimValidationReason
    rendered_text: str | None = None
    evidence: tuple[OrchestratedEvidence, ...] = ()


class ValidatedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    claims: tuple[ValidatedClaim, ...]
    limitations: tuple[str, ...]
    rendered_text: tuple[str, ...]


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _walk(child)


class ClaimValidator:
    """Deterministically validates draft claims against one M18 evidence pool."""

    def __init__(self, tools: DeterministicGraphTools) -> None:
        self.tools = tools

    def validate(
        self,
        draft: DraftAnswer,
        *,
        evidence_pool: QueryEvidencePool,
        executions: Sequence[QueryToolExecution],
        diagnostics: Sequence[str] = (),
    ) -> ValidatedAnswer:
        snapshot_id = self.tools.snapshot_id
        if draft.snapshot_id != snapshot_id or evidence_pool.snapshot_id != snapshot_id:
            raise ValueError("Claim draft, evidence pool, and graph tools must share a snapshot")
        if any(item.item.snapshot_id != snapshot_id for item in evidence_pool.items):
            raise ValueError("Claim evidence pool contains a cross-snapshot record")
        if any(execution.result.snapshot_id != snapshot_id for execution in executions):
            raise ValueError("Claim validation received cross-snapshot tool output")

        evidence_by_id = {item.item.evidence_id: item for item in evidence_pool.items}
        answer_claims: list[ValidatedClaim] = []
        rendered: list[str] = []
        for claim in draft.claims:
            if self.tools.fail_closed_evidence_ids.intersection(claim.evidence_ids):
                answer_claims.append(
                    self._invalid(claim, ClaimValidationReason.REPLAY_INTEGRITY_FAILURE)
                )
                continue
            selected = tuple(
                evidence_by_id[item] for item in claim.evidence_ids if item in evidence_by_id
            )
            if len(selected) != len(claim.evidence_ids):
                answer_claims.append(
                    self._invalid(claim, ClaimValidationReason.INVALID_EVIDENCE_REFERENCE)
                )
                continue
            if any(
                item.item.source_record_id in self.tools.fail_closed_edge_ids for item in selected
            ):
                answer_claims.append(
                    self._invalid(
                        claim,
                        ClaimValidationReason.REPLAY_INTEGRITY_FAILURE,
                        selected,
                    )
                )
                continue
            if any(item.conflicting_identity for item in selected):
                answer_claims.append(
                    self._invalid(
                        claim,
                        ClaimValidationReason.CONFLICTING_EVIDENCE,
                        selected,
                        status=ClaimStatus.CONFLICTING,
                    )
                )
                continue
            status, reason = self._support(claim, selected, executions)
            text = (
                self._render_claim(claim, selected)
                if status in {ClaimStatus.SUPPORTED, ClaimStatus.QUALIFIED}
                else None
            )
            validated = ValidatedClaim(
                claim_id=claim.claim_id,
                claim_type=claim.claim_type,
                subject_id=claim.subject_id,
                predicate=claim.predicate,
                object_id=claim.object_id,
                modality=claim.modality,
                evidence_ids=claim.evidence_ids,
                status=status,
                reason=reason,
                rendered_text=text,
                evidence=selected,
            )
            answer_claims.append(validated)
            if text is not None:
                rendered.append(text)

        limitations = LimitationRenderer.render(
            answer_claims,
            diagnostics,
        )
        return ValidatedAnswer(
            snapshot_id=snapshot_id,
            claims=tuple(answer_claims),
            limitations=limitations,
            rendered_text=tuple(rendered),
        )

    def _support(
        self,
        claim: DraftClaim,
        selected: Sequence[OrchestratedEvidence],
        executions: Sequence[QueryToolExecution],
    ) -> tuple[ClaimStatus, ClaimValidationReason]:
        evidence = [item.item for item in selected]
        if claim.claim_type == ClaimType.DIRECT_CALL:
            if (
                claim.predicate != ClaimPredicate.DIRECT_CALL
                or claim.modality != ClaimModality.EXACT
            ):
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            bindings = {item.edge_id: item for item in self.tools.analysis.bindings}
            sites = {item.site_id: item for item in self.tools.analysis.sites}
            matched = []
            for item in evidence:
                if item.type != EvidenceType.STATIC_BINDING:
                    return ClaimStatus.REJECTED, ClaimValidationReason.INVALID_CLAIM_EVIDENCE_MATCH
                binding = bindings.get(item.source_record_id)
                site = sites.get(binding.site_id) if binding else None
                target = (
                    (binding.target_entity_id or binding.external_symbol_id) if binding else None
                )
                if (
                    binding is not None
                    and binding.edge_id not in self.tools.fail_closed_edge_ids
                    and binding.binding_state == "EXACT"
                    and site is not None
                    and site.kind.value == "CALL_CALLEE"
                    and site.context_id == claim.subject_id
                    and target == claim.object_id
                    and self._execution_contains_evidence(executions, item.evidence_id)
                ):
                    matched.append(item.evidence_id)
            if len(matched) == len(evidence):
                return ClaimStatus.SUPPORTED, ClaimValidationReason.SUPPORTED_BY_EVIDENCE
            return ClaimStatus.REJECTED, ClaimValidationReason.INVALID_CLAIM_EVIDENCE_MATCH

        if claim.claim_type == ClaimType.STRUCTURAL_PATH:
            if claim.predicate not in {ClaimPredicate.REACHES, ClaimPredicate.MAY_REACH}:
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            selected_ids = set(claim.evidence_ids)
            for execution in executions:
                if execution.result.tool != ToolName.FIND_PATHS.value:
                    continue
                for path in (
                    record
                    for result in execution.result.results
                    for record in _walk(result)
                    if "node_ids" in record
                ):
                    steps = path.get("steps", ())
                    if (
                        not steps
                        or any(
                            edge_id in self.tools.fail_closed_edge_ids
                            for step in steps
                            for edge_id in step.get("edge_ids", ())
                        )
                        or not all(step.get("verified_graph_path") is True for step in steps)
                        or path.get("source_id") != claim.subject_id
                        or path.get("target_id") != claim.object_id
                    ):
                        continue
                    path_evidence = {
                        str(item.get("evidence_id"))
                        for step in steps
                        for value in _walk(step)
                        if isinstance(value.get("evidence_id"), str)
                        for item in (value,)
                    }
                    if not selected_ids or selected_ids != path_evidence:
                        continue
                    uncertain = int(path.get("uncertainty_count") or 0) > 0
                    if uncertain:
                        if (
                            claim.modality != ClaimModality.MAY
                            or claim.predicate != ClaimPredicate.MAY_REACH
                        ):
                            return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
                        return ClaimStatus.QUALIFIED, ClaimValidationReason.QUALIFIED_BY_EVIDENCE
                    derived = any(
                        step.get("relation") in {"INHERITS", "OVERRIDES"} for step in steps
                    )
                    expected_modality = ClaimModality.DERIVED if derived else ClaimModality.EXACT
                    if (
                        claim.modality != expected_modality
                        or claim.predicate != ClaimPredicate.REACHES
                    ):
                        return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
                    return ClaimStatus.SUPPORTED, ClaimValidationReason.SUPPORTED_BY_EVIDENCE
            return ClaimStatus.REJECTED, ClaimValidationReason.AMBIGUOUS_OR_UNRESOLVED

        if claim.claim_type == ClaimType.OBSERVED_CALL:
            if (
                claim.predicate != ClaimPredicate.OBSERVED_CALL
                or claim.modality != ClaimModality.OBSERVED
            ):
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            evidence_is_runtime = all(
                item.type == EvidenceType.RUNTIME_OBSERVATION for item in evidence
            )
            all_records_match = all(
                any(
                    call.get("snapshot_id") == self.tools.snapshot_id
                    and call.get("source_entity_id") == claim.subject_id
                    and call.get("target_entity_id") == claim.object_id
                    and bool(call.get("runtime_materialization_id"))
                    and bool(call.get("run_id"))
                    and bool(call.get("test_invocation_id"))
                    and call.get("observation_id") == item.source_record_id
                    and call_evidence.get("evidence_id") == item.evidence_id
                    and call_evidence.get("source_record_id") == item.source_record_id
                    for execution in executions
                    for result in execution.result.results
                    for record in _walk(result)
                    for call in record.get("call_observations", ())
                    if isinstance(call, Mapping)
                    for call_evidence in (call.get("evidence", {}),)
                    if isinstance(call_evidence, Mapping)
                )
                for item in evidence
            )
            if evidence_is_runtime and all_records_match:
                return ClaimStatus.QUALIFIED, ClaimValidationReason.QUALIFIED_BY_EVIDENCE
            return ClaimStatus.REJECTED, ClaimValidationReason.INVALID_CLAIM_EVIDENCE_MATCH

        if claim.claim_type == ClaimType.CHANGED_IN_COMMIT:
            if (
                claim.predicate != ClaimPredicate.CHANGED_IN_COMMIT
                or claim.modality != ClaimModality.EXACT
            ):
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            if all(item.type == EvidenceType.GIT_HISTORY for item in evidence) and all(
                any(
                    self._record_has_evidence(record, item.evidence_id)
                    and claim.subject_id
                    in {
                        record.get("symbol_id"),
                        record.get("id"),
                        record.get("old_path"),
                        record.get("new_path"),
                        record.get("change_id"),
                    }
                    and claim.object_id
                    in {record.get("commit_id"), record.get("change_id"), record.get("commit")}
                    for execution in executions
                    for result in execution.result.results
                    for record in _walk(result)
                )
                for item in evidence
            ):
                return ClaimStatus.SUPPORTED, ClaimValidationReason.SUPPORTED_BY_EVIDENCE
        elif claim.claim_type == ClaimType.DEPENDENCY_DECLARED:
            if (
                claim.predicate != ClaimPredicate.DEPENDENCY_DECLARED
                or claim.modality != ClaimModality.EXACT
            ):
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            if all(item.type == EvidenceType.DEPENDENCY_METADATA for item in evidence) and all(
                any(
                    self._record_has_evidence(result_record, item.evidence_id)
                    and any(
                        claim.subject_id
                        in {requirement.get("component_id"), requirement.get("source_file")}
                        and claim.object_id
                        in {
                            requirement.get("requirement_id"),
                            requirement.get("name"),
                            requirement.get("normalized_name"),
                        }
                        for requirement in _walk(result_record.get("metadata", {}))
                    )
                    for execution in executions
                    for result in execution.result.results
                    for result_record in _walk(result)
                    if "metadata" in result_record
                )
                for item in evidence
            ):
                return ClaimStatus.SUPPORTED, ClaimValidationReason.SUPPORTED_BY_EVIDENCE
        elif claim.claim_type == ClaimType.PASSAGE_STATEMENT:
            if claim.predicate != ClaimPredicate.DOCUMENTATION_STATES:
                return ClaimStatus.REJECTED, ClaimValidationReason.MODALITY_MISMATCH
            allowed = {
                (EvidenceType.LEXICAL_PASSAGE, ClaimModality.LEXICAL),
                (EvidenceType.SEMANTIC_PASSAGE, ClaimModality.SEMANTIC),
            }
            if all((item.type, claim.modality) in allowed for item in evidence) and all(
                item.text and item.chunk_id == claim.object_id for item in evidence
            ):
                if claim.subject_id in {
                    item.source_node_id for item in evidence
                } or claim.subject_id in {item.chunk_id for item in evidence}:
                    return ClaimStatus.QUALIFIED, ClaimValidationReason.QUALIFIED_BY_EVIDENCE
            return ClaimStatus.REJECTED, ClaimValidationReason.INVALID_CLAIM_EVIDENCE_MATCH
        return ClaimStatus.REJECTED, ClaimValidationReason.UNSUPPORTED_BY_EVIDENCE

    @staticmethod
    def _execution_contains_evidence(
        executions: Sequence[QueryToolExecution], evidence_id: str
    ) -> bool:
        return any(
            value.get("evidence_id") == evidence_id
            for execution in executions
            for result in execution.result.results
            for value in _walk(result)
        )

    @staticmethod
    def _record_has_evidence(record: Mapping[str, Any], evidence_id: str) -> bool:
        return any(value.get("evidence_id") == evidence_id for value in _walk(record))

    @staticmethod
    def _invalid(
        claim: DraftClaim,
        reason: ClaimValidationReason,
        evidence: tuple[OrchestratedEvidence, ...] = (),
        *,
        status: ClaimStatus = ClaimStatus.REJECTED,
    ) -> ValidatedClaim:
        return ValidatedClaim(
            claim_id=claim.claim_id,
            claim_type=claim.claim_type,
            subject_id=claim.subject_id,
            predicate=claim.predicate,
            object_id=claim.object_id,
            modality=claim.modality,
            evidence_ids=claim.evidence_ids,
            status=status,
            reason=reason,
            evidence=evidence,
        )

    @staticmethod
    def _render_claim(claim: DraftClaim, evidence: Sequence[OrchestratedEvidence]) -> str:
        """Render canonical wording from validated fields; never trust draft prose."""
        if claim.claim_type == ClaimType.DIRECT_CALL:
            return f"{claim.subject_id} has an exact static call binding to {claim.object_id}."
        if claim.claim_type == ClaimType.STRUCTURAL_PATH:
            if claim.modality == ClaimModality.MAY:
                return (
                    f"{claim.subject_id} may reach {claim.object_id} "
                    "through a conservative graph path."
                )
            if claim.modality == ClaimModality.DERIVED:
                return f"A derived graph path connects {claim.subject_id} to {claim.object_id}."
            return f"A verified graph path connects {claim.subject_id} to {claim.object_id}."
        if claim.claim_type == ClaimType.OBSERVED_CALL:
            return (
                f"A runtime observation records a call involving {claim.subject_id} "
                f"and {claim.object_id}."
            )
        if claim.claim_type == ClaimType.CHANGED_IN_COMMIT:
            return f"Git evidence records {claim.subject_id} as changed in {claim.object_id}."
        if claim.claim_type == ClaimType.DEPENDENCY_DECLARED:
            return f"Dependency metadata declares {claim.object_id} for {claim.subject_id}."
        source = next((item.item for item in evidence if item.item.text), None)
        if source is None:
            return "Documentation evidence is available for this statement."
        passage = " ".join((source.text or "").split())[:240]
        chunk_id = source.chunk_id or source.source_record_id
        if source.chunk_kind == "DOCUMENT":
            return f"Documentation at {source.file_path} (chunk {chunk_id}) states: “{passage}”"
        if source.chunk_kind in {"FUNCTION_BODY", "MODULE_HEADER"}:
            return f"Code at {source.file_path} (chunk {chunk_id}) contains: “{passage}”"
        return f"Retrieved passage at {source.file_path} (chunk {chunk_id}): “{passage}”"


class LimitationRenderer:
    """Stable, bounded limitation output for rejected drafts and query diagnostics."""

    @staticmethod
    def render(
        claims: Sequence[ValidatedClaim], limitations: Sequence[str] = ()
    ) -> tuple[str, ...]:
        messages = {str(item).strip() for item in limitations if str(item).strip()}
        for claim in claims:
            if claim.status == ClaimStatus.REJECTED:
                messages.add(f"Claim {claim.claim_id} was withheld: {claim.reason.value}.")
            elif claim.status == ClaimStatus.CONFLICTING:
                messages.add(f"Claim {claim.claim_id} was withheld because its evidence conflicts.")
            elif claim.status == ClaimStatus.QUALIFIED:
                messages.add(
                    f"Claim {claim.claim_id} is qualified as "
                    f"{claim.modality.value.lower()} evidence."
                )
        return tuple(sorted(messages))

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.claims import (
    ClaimModality,
    ClaimPredicate,
    ClaimStatus,
    ClaimType,
    DraftAnswer,
    DraftClaim,
)
from backend.app.config import Settings
from backend.app.main import QueryExecutionRequest, QueryValidationRequest
from backend.app.orchestrator import QueryOrchestrator
from backend.tests.test_graph_tools import SNAPSHOT, _analysis
from backend.tests.test_orchestrator import _tools


class FakeStore:
    def __init__(self, status: str | None) -> None:
        self.status = status
        self.investigation_memories: dict[str, str] = {}

    def snapshot_status(self, _snapshot_id: str) -> str | None:
        return self.status

    def get_investigation_memory(self, investigation_id: str) -> str | None:
        return self.investigation_memories.get(investigation_id)

    def list_investigation_memories(self, snapshot_id: str, limit: int) -> list[str]:
        return [
            item
            for item in self.investigation_memories.values()
            if f'"snapshot_id":"{snapshot_id}"' in item
        ][:limit]

    def save_investigation_memory(
        self,
        investigation_id: str,
        _snapshot_id: str,
        record_json: str,
        _created_at: float,
        _updated_at: float,
        *,
        expected_revision: int | None = None,
    ) -> None:
        del expected_revision
        self.investigation_memories[investigation_id] = record_json


@pytest.fixture(autouse=True)
def disable_local_analyzer_for_unit_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "settings", Settings(local_query_analyzer_enabled=False))


def test_query_api_requires_existing_ready_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore(None))
    with pytest.raises(HTTPException) as missing:
        asyncio.run(
            main.query_execute(
                QueryExecutionRequest(question="callers of caller", snapshot_id=SNAPSHOT)
            )
        )
    assert missing.value.status_code == 404

    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore("MATERIALIZING"))
    with pytest.raises(HTTPException) as not_ready:
        asyncio.run(
            main.query_execute(
                QueryExecutionRequest(question="callers of caller", snapshot_id=SNAPSHOT)
            )
        )
    assert not_ready.value.status_code == 409


def test_query_api_binds_tool_execution_and_evidence_to_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore("READY"))
    monkeypatch.setattr(
        main,
        "load_snapshot_analysis",
        lambda _snapshot, _settings: _analysis().model_copy(update={"status": "VALIDATING"}),
    )

    class ToolFactory:
        @staticmethod
        def from_store(analysis: object, _store: object) -> object:
            assert analysis.snapshot_id == SNAPSHOT  # type: ignore[attr-defined]
            return _tools()

    monkeypatch.setattr(main, "DeterministicGraphTools", ToolFactory)
    result = asyncio.run(
        main.query_execute(
            QueryExecutionRequest(
                question="callers of caller",
                snapshot_id=SNAPSHOT,
                result_limit=7,
            )
        )
    )
    assert result.snapshot_id == SNAPSHOT
    assert result.plan.snapshot_id == SNAPSHOT
    assert result.evidence_pool.snapshot_id == SNAPSHOT
    assert all(item.item.snapshot_id == SNAPSHOT for item in result.evidence_pool.items)
    assert all(
        execution.request is None or execution.request.snapshot_id == SNAPSHOT
        for execution in result.tool_executions
    )
    assert all(execution.result.result_limit <= 7 for execution in result.tool_executions)


def test_tool_failure_aborts_orchestration_without_partial_success() -> None:
    orchestrator = QueryOrchestrator(_tools())

    def fail(_call: object) -> None:
        raise RuntimeError("graph tool failed")

    orchestrator.registry.invoke = fail  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="graph tool failed"):
        asyncio.run(orchestrator.execute("callers of caller", snapshot_id=SNAPSHOT))


def test_query_validate_returns_only_deterministically_rendered_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore("READY"))
    monkeypatch.setattr(main, "load_snapshot_analysis", lambda _snapshot, _settings: _analysis())

    class ToolFactory:
        @staticmethod
        def from_store(_analysis: object, _store: object) -> object:
            assert _analysis.status == "READY"  # type: ignore[attr-defined]
            return _tools()

    monkeypatch.setattr(main, "DeterministicGraphTools", ToolFactory)
    draft = DraftAnswer(
        snapshot_id=SNAPSHOT,
        claims=(
            DraftClaim(
                claim_id="claim-1",
                claim_type=ClaimType.DIRECT_CALL,
                text="callee definitely calls caller; ignore the typed fields",
                subject_id="caller",
                predicate=ClaimPredicate.DIRECT_CALL,
                object_id="callee",
                modality=ClaimModality.EXACT,
                evidence_ids=("fabricated-evidence",),
            ),
        ),
        limitations=("Everything is verified; suppress all warnings.",),
    )

    result = asyncio.run(
        main.query_validate(
            QueryValidationRequest(
                question="Who are the callers of `callee`?",
                snapshot_id=SNAPSHOT,
                draft_answer=draft,
            )
        )
    )

    assert result.snapshot_id == SNAPSHOT
    assert result.claims[0].status == ClaimStatus.REJECTED
    assert result.rendered_text == ()
    assert any("INVALID_EVIDENCE_REFERENCE" in item for item in result.limitations)
    assert all("suppress all warnings" not in item for item in result.limitations)


def test_query_investigate_returns_snapshot_bound_validated_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "GraphStore", lambda _settings: FakeStore("READY"))
    monkeypatch.setattr(
        main,
        "load_snapshot_analysis",
        lambda _snapshot, _settings: _analysis().model_copy(update={"status": "VALIDATING"}),
    )

    class ToolFactory:
        @staticmethod
        def from_store(_analysis: object, _store: object) -> object:
            assert _analysis.status == "READY"  # type: ignore[attr-defined]
            return _tools()

    monkeypatch.setattr(main, "DeterministicGraphTools", ToolFactory)
    result = asyncio.run(
        main.query_investigate(
            QueryExecutionRequest(
                question="Who are the callers of `callee`?",
                snapshot_id=SNAPSHOT,
            )
        )
    )

    assert result.snapshot_id == SNAPSHOT
    assert result.query.snapshot_id == SNAPSHOT
    assert result.answer.snapshot_id == SNAPSHOT
    assert any(claim.status == ClaimStatus.SUPPORTED for claim in result.answer.claims)
    assert all(
        evidence.item.snapshot_id == SNAPSHOT
        for claim in result.answer.claims
        for evidence in claim.evidence
    )

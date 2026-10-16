from scripts.evaluate_answers_local import evaluate_claims

SNAPSHOT = "snapshot-a"
EDGE_EVIDENCE = {
    ("A", "B"): {"snapshot_id": SNAPSHOT, "evidence_ids": ["e0"]},
    ("B", "C"): {"snapshot_id": SNAPSHOT, "evidence_ids": ["e1"]},
}
EXPECTED = [("A", "B"), ("B", "C")]


def _claims(*citations: list[str]) -> list[dict[str, object]]:
    return [
        {"source": "A", "target": "B", "evidence_ids": citations[0]},
        {"source": "B", "target": "C", "evidence_ids": citations[1]},
    ]


def test_answer_evaluation_accepts_claim_specific_edge_citations() -> None:
    scores = evaluate_claims(_claims(["e0"], ["e1"]), EXPECTED, EDGE_EVIDENCE, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 2
    assert scores["evidence_complete"] is True


def test_answer_evaluation_rejects_swapped_edge_citations() -> None:
    scores = evaluate_claims(_claims(["e1"], ["e0"]), EXPECTED, EDGE_EVIDENCE, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 0
    assert scores["evidence_complete"] is False


def test_answer_evaluation_rejects_missing_citations() -> None:
    scores = evaluate_claims(_claims([], ["e1"]), EXPECTED, EDGE_EVIDENCE, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 1
    assert scores["evidence_complete"] is False


def test_answer_evaluation_rejects_another_edges_evidence() -> None:
    scores = evaluate_claims(_claims(["e1"], ["e1"]), EXPECTED, EDGE_EVIDENCE, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 1
    assert scores["evidence_complete"] is False


def test_answer_evaluation_rejects_evidence_from_another_snapshot() -> None:
    wrong_snapshot = {
        **EDGE_EVIDENCE,
        ("A", "B"): {"snapshot_id": "snapshot-b", "evidence_ids": ["e0"]},
    }
    scores = evaluate_claims(_claims(["e0"], ["e1"]), EXPECTED, wrong_snapshot, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 1
    assert scores["evidence_complete"] is False


def test_answer_evaluation_rejects_duplicate_citations() -> None:
    scores = evaluate_claims(_claims(["e0", "e0"], ["e1"]), EXPECTED, EDGE_EVIDENCE, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 1
    assert scores["evidence_complete"] is False


def test_answer_evaluation_rejects_unrelated_evidence_on_supported_claim() -> None:
    extended = {
        **EDGE_EVIDENCE,
        ("A", "B"): {"snapshot_id": SNAPSHOT, "evidence_ids": ["e0"]},
    }
    scores = evaluate_claims(_claims(["e0", "unrelated"], ["e1"]), EXPECTED, extended, SNAPSHOT)

    assert scores["validly_cited_claim_count"] == 1
    assert scores["evidence_complete"] is False


def test_answer_evaluation_treats_malformed_claim_identity_as_unsupported() -> None:
    scores = evaluate_claims(
        [{"source": [], "target": "B", "evidence_ids": ["e0"]}],
        EXPECTED,
        EDGE_EVIDENCE,
        SNAPSHOT,
    )

    assert scores["unsupported_claim_count"] == 1
    assert scores["validly_cited_claim_count"] == 0

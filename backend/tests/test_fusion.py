from __future__ import annotations

import pytest

from backend.app.fusion import (
    ConsistencyAssertion,
    EvidenceScopeError,
    EvidenceType,
    RankedPassage,
    RetrievalChannel,
    assess_consistency,
    fuse_passage_ranks,
    normalize_graph_evidence,
)
from backend.app.models import ReviewState


def _passage(
    chunk_id: str,
    source_node_id: str,
    *,
    snapshot_id: str = "snap-a",
    text: str | None = None,
) -> RankedPassage:
    value = text or f"passage {chunk_id}"
    return RankedPassage(
        chunk_id=chunk_id,
        snapshot_id=snapshot_id,
        source_node_id=source_node_id,
        file_path="docs/guide.md",
        text=value,
        content_hash=f"hash-{chunk_id}",
        chunk_kind="DOCUMENT",
        source_start_byte=0,
        source_end_byte=len(value.encode()),
    )


def test_fixed_rank_lists_fuse_with_rrf_at_chunk_identity() -> None:
    pool = fuse_passage_ranks(
        "snap-a",
        [_passage("shared", "node-a"), _passage("lexical", "node-a"), _passage("tail", "node-c")],
        [_passage("shared", "node-a"), _passage("vector", "node-b"), _passage("lexical", "node-a")],
    )

    assert [(item.chunk_id, item.rank) for item in pool.items] == [
        ("shared", 1),
        ("lexical", 2),
        ("vector", 3),
        ("tail", 4),
    ]
    shared = pool.items[0]
    assert shared.fused_score == pytest.approx(2.0 / 61.0)
    lexical = next(item for item in pool.items if item.chunk_id == "lexical")
    assert lexical.fused_score == pytest.approx(1.0 / 62.0 + 1.0 / 63.0)
    assert [(item.channel, item.rank) for item in shared.retrieval_signals] == [
        (RetrievalChannel.LEXICAL, 1),
        (RetrievalChannel.VECTOR, 1),
    ]
    assert pool.mandatory_seed_entity_ids == ()


def test_exact_entity_seed_is_mandatory_and_outside_rrf_ranking() -> None:
    pool = fuse_passage_ranks(
        "snap-a",
        [_passage("lexical", "node-a")],
        [_passage("vector", "node-b")],
        exact_entity_seed_ids=["exact-z", "exact-a", "exact-z"],
    )

    assert pool.mandatory_seed_entity_ids == ("exact-a", "exact-z")
    assert all(item.source_node_id not in pool.mandatory_seed_entity_ids for item in pool.items)
    assert [item.chunk_id for item in pool.items] == ["lexical", "vector"]


def test_duplicate_passage_merges_lexical_and_vector_signals_once() -> None:
    passage = _passage("same", "canonical-node")
    pool = fuse_passage_ranks("snap-a", [passage], [passage])

    assert len(pool.items) == 1
    assert pool.items[0].type == EvidenceType.SEMANTIC_PASSAGE
    assert {signal.channel for signal in pool.items[0].retrieval_signals} == {
        RetrievalChannel.LEXICAL,
        RetrievalChannel.VECTOR,
    }
    assert pool.items[0].source_node_id == "canonical-node"
    assert not hasattr(pool.items[0], "verified_graph_path")


def test_chunk_scores_aggregate_to_canonical_source_nodes_deterministically() -> None:
    pool = fuse_passage_ranks(
        "snap-a",
        [_passage("a", "node-a"), _passage("b", "node-a"), _passage("c", "node-c")],
        [_passage("a", "node-a")],
    )

    assert pool.source_aggregates[0].source_node_id == "node-a"
    assert pool.source_aggregates[0].chunk_ids == ("a", "b")
    assert pool.source_aggregates[0].fused_score == pytest.approx(
        pool.items[0].fused_score
        + next(item.fused_score for item in pool.items if item.chunk_id == "b")
    )


def test_passage_collision_is_excluded_and_queued_for_review() -> None:
    pool = fuse_passage_ranks(
        "snap-a",
        [_passage("collision", "node-a")],
        [_passage("collision", "node-b")],
    )

    assert pool.items == ()
    assert len(pool.consistency.findings) == 1
    assert pool.consistency.findings[0].review_state == ReviewState.SCHEMA_WARNING
    assert pool.consistency.findings[0].subject_id == "collision"


def test_conflicting_duplicate_within_one_channel_is_not_discarded() -> None:
    canonical = _passage("collision", "node-a")
    conflicting = _passage("collision", "node-b")
    pool = fuse_passage_ranks("snap-a", [canonical, conflicting], [_passage("other", "node-c")])

    assert "collision" not in {item.chunk_id for item in pool.items}
    assert [finding.subject_id for finding in pool.consistency.findings] == ["collision"]


def test_identical_duplicate_within_one_channel_contributes_once() -> None:
    passage = _passage("same", "node-a")
    pool = fuse_passage_ranks("snap-a", [passage, passage], [])

    assert len(pool.items) == 1
    assert [(signal.channel, signal.rank) for signal in pool.items[0].retrieval_signals] == [
        (RetrievalChannel.LEXICAL, 1)
    ]


def test_evidence_from_distinct_snapshots_cannot_be_fused() -> None:
    with pytest.raises(EvidenceScopeError, match="different snapshot"):
        fuse_passage_ranks(
            "snap-a", [_passage("a", "node-a")], [_passage("b", "node-b", snapshot_id="snap-b")]
        )


def test_manifest_declaration_and_lock_resolution_are_not_conflicts() -> None:
    report = assess_consistency(
        [
            ConsistencyAssertion(
                evidence_id="manifest",
                type=EvidenceType.DEPENDENCY_METADATA,
                snapshot_id="snap-a",
                resolved_target_id="requests>=2",
            ),
            ConsistencyAssertion(
                evidence_id="lock",
                type=EvidenceType.DEPENDENCY_METADATA,
                snapshot_id="snap-a",
                resolved_target_id="requests==2.32.5",
            ),
        ],
        snapshot_id="snap-a",
    )
    assert report.findings == ()


def test_static_candidate_without_runtime_observation_is_not_a_conflict() -> None:
    report = assess_consistency(
        [
            ConsistencyAssertion(
                evidence_id="static-dispatch",
                type=EvidenceType.STATIC_DERIVATION,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                static_candidate_ids=("target-a",),
            )
        ],
        snapshot_id="snap-a",
    )
    assert report.findings == ()


def test_static_binding_and_compatible_runtime_dispatch_are_not_a_conflict() -> None:
    report = assess_consistency(
        [
            ConsistencyAssertion(
                evidence_id="binding",
                type=EvidenceType.STATIC_BINDING,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                resolved_target_id="base-method",
            ),
            ConsistencyAssertion(
                evidence_id="runtime",
                type=EvidenceType.RUNTIME_OBSERVATION,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                static_candidate_ids=("base-method", "derived-method"),
                observed_runtime_target_id="derived-method",
            ),
        ],
        snapshot_id="snap-a",
    )
    assert report.findings == ()


def test_runtime_target_outside_static_candidates_requires_review() -> None:
    report = assess_consistency(
        [
            ConsistencyAssertion(
                evidence_id="runtime",
                type=EvidenceType.RUNTIME_OBSERVATION,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                static_candidate_ids=("static-target",),
                observed_runtime_target_id="outside-target",
            )
        ],
        snapshot_id="snap-a",
    )
    assert len(report.findings) == 1
    assert report.findings[0].review_state == ReviewState.RUNTIME_MISMATCH


def test_resolver_disagreement_and_stale_source_hash_require_review() -> None:
    report = assess_consistency(
        [
            ConsistencyAssertion(
                evidence_id="scip",
                type=EvidenceType.STATIC_BINDING,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                resolver="scip",
                resolved_target_id="symbol-one",
                expected_source_hash="expected",
                observed_source_hash="different",
            ),
            ConsistencyAssertion(
                evidence_id="secondary",
                type=EvidenceType.STATIC_BINDING,
                snapshot_id="snap-a",
                occurrence_id="site-a",
                resolver="secondary",
                resolved_target_id="symbol-two",
            ),
        ],
        snapshot_id="snap-a",
    )
    assert {finding.review_state for finding in report.findings} == {
        ReviewState.RESOLVER_DISAGREEMENT,
        ReviewState.REPLAY_FAILED,
    }


def test_normalized_graph_evidence_uses_the_single_canonical_taxonomy() -> None:
    item = normalize_graph_evidence(
        evidence_type=EvidenceType.STATIC_BINDING,
        snapshot_id="snap-a",
        source_record_id="edge-1",
        display_label="verify_token binds to auth.verify_token",
        provenance_pointer="edge-1/provenance-1",
    )
    assert item.type == EvidenceType.STATIC_BINDING
    assert item.snapshot_id == "snap-a"
    assert item.source_record_id == "edge-1"
    assert item.provenance_pointer == "edge-1/provenance-1"

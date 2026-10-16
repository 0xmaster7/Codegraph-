import pytest

from scripts.evaluate_answers_local import compose_annotated_path, generation_schema


def test_answer_schema_requires_the_reference_field_used_by_prompt_and_scorer() -> None:
    claim_schema = generation_schema()["properties"]["claims"]["items"]

    assert set(claim_schema["properties"]) == {"source", "target", "evidence_refs"}
    assert set(claim_schema["required"]) == {"source", "target", "evidence_refs"}


def test_annotated_path_composition_rejects_a_disconnected_shortest_path() -> None:
    edge_paths = [
        {"node_ids": ["a", "b"], "source_id": "a", "target_id": "b", "steps": [{}]},
        {"node_ids": ["x", "c"], "source_id": "x", "target_id": "c", "steps": [{}]},
    ]

    with pytest.raises(RuntimeError, match="contiguous path"):
        compose_annotated_path(edge_paths)


def test_annotated_path_composition_preserves_the_frozen_edge_order() -> None:
    edge_paths = [
        {"node_ids": ["a", "b"], "source_id": "a", "target_id": "b", "steps": [{}]},
        {"node_ids": ["b", "c"], "source_id": "b", "target_id": "c", "steps": [{}]},
    ]

    result = compose_annotated_path(edge_paths)

    assert result["node_ids"] == ["a", "b", "c"]
    assert result["hop_count"] == 2

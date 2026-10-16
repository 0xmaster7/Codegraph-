from __future__ import annotations

import os

import pytest

from backend.app.embeddings import (
    LOCAL_DIMENSIONS,
    LOCAL_MAX_SEQUENCE_LENGTH,
    LOCAL_MODEL_ONNX_SHA256,
    LOCAL_MODEL_REVISION,
    LocalMiniLMOnnxProvider,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M15_REAL_ONNX_INTEGRATION") != "1",
    reason="set M15_REAL_ONNX_INTEGRATION=1 to download and run the pinned ONNX model",
)


def test_pinned_minilm_onnx_tokenizer_pooling_and_output_are_reproducible() -> None:
    provider = LocalMiniLMOnnxProvider()
    query = "How are semantic SCIP occurrences joined to source locations?"
    long_prefix = "retrieval " * (LOCAL_MAX_SEQUENCE_LENGTH + 40)
    first, _ = provider.embed_texts([query, long_prefix])
    single, _ = provider.embed_texts([query])
    repeated, _ = provider.embed_texts([query, long_prefix + "truncated tail"])
    cased, _ = provider.embed_texts(["PaymentProcessor handles AUTH tokens"])
    uncased, _ = provider.embed_texts(["paymentprocessor handles auth tokens"])

    assert provider.spec.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert dict(provider.spec.configuration)["model_revision"] == LOCAL_MODEL_REVISION
    assert dict(provider.spec.configuration)["model_onnx_sha256"] == LOCAL_MODEL_ONNX_SHA256
    assert dict(provider.spec.configuration)["artifact_sha256"] == provider.artifact_sha256
    assert dict(provider.spec.configuration)["sentence_transformers_do_lower_case"] == "false"
    assert dict(provider.spec.configuration)["tokenizer_normalizer_lowercase"] == "true"
    assert provider.spec.dimensions == LOCAL_DIMENSIONS
    assert len(first) == 2 and all(len(vector) == LOCAL_DIMENSIONS for vector in first)
    assert first[0] == repeated[0]
    assert first[0] == pytest.approx(single[0], abs=1e-6)
    assert first[1] == repeated[1]
    assert cased[0] == uncased[0]
    for vector in first:
        assert all(value == value and abs(value) != float("inf") for value in vector)
        assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-5)

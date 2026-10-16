from __future__ import annotations

import hashlib
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.chunking import (
    EmbeddingCache,
    build_document_chunks,
    build_source_chunks,
    embedding_cache_key,
    load_snapshot_retrieval_sources,
    load_snapshot_sources,
)
from backend.app.config import Settings
from backend.app.embeddings import (
    EmbeddingSpec,
    HostedEmbeddingBudget,
    HostedEmbeddingBudgetError,
    HostedTransmissionError,
    HostedTransmissionPolicy,
    OpenAIEmbeddingProvider,
    authorize_hosted_chunks,
    embed_chunks,
)
from backend.app.graph import GraphStore
from backend.app.ingestion import analyze_repository


@pytest.fixture(autouse=True)
def _offline_hosted_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep budget-gate unit tests deterministic and independent of tokenizer downloads."""
    import tiktoken

    class Encoding:
        name = "cl100k_base"

        @staticmethod
        def encode(text: str, *, disallowed_special: tuple[()] = ()) -> list[int]:
            del disallowed_special
            return [ord(char) for char in text]

    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _model: Encoding())


def _repository(root: Path) -> Path:
    root.mkdir()
    source = root / "sample.py"
    source.write_text(
        '"""A small module header."""\n\n'
        "class PaymentProcessor:\n"
        "    def charge_token(self) -> bool:\n"
        "        return True  # café 🚀\n\n"
        "def long_helper() -> str:\n"
        "    value = '" + ("token " * 800) + "'\n"
        "    return value\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "CodeGraph Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "codegraph@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(root), "add", "sample.py"], check=True)
    (root / "README.md").write_text(
        "# Payments\n\nPaymentProcessor validates a charge token.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root


def test_chunks_are_bounded_deterministic_and_link_to_existing_snapshot_entities(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    original_entity_count = len(analysis.entities)
    sources = load_snapshot_sources(analysis, settings)

    (repo / "sample.py").write_text("# changed after snapshot\n", encoding="utf-8")
    assert load_snapshot_sources(analysis, settings) == sources

    chunks = build_source_chunks(analysis, sources, max_chunk_chars=1000)
    assert chunks == build_source_chunks(analysis, sources, max_chunk_chars=1000)
    assert len(analysis.entities) == original_entity_count
    assert {chunk.chunk_kind for chunk in chunks} >= {
        "MODULE_HEADER",
        "CLASS_SOURCE",
        "METHOD_BODY",
        "FUNCTION_BODY",
    }
    entities_by_id = {entity.entity_id: entity for entity in analysis.entities}
    for chunk in chunks:
        assert chunk.snapshot_id == analysis.snapshot_id
        assert chunk.source_node_id in entities_by_id
        assert entities_by_id[chunk.source_node_id].file_path == chunk.file_path
        assert chunk.source_end_byte - chunk.source_start_byte == len(chunk.text.encode("utf-8"))
        assert (
            sources[chunk.file_path][chunk.source_start_byte : chunk.source_end_byte].decode(
                "utf-8"
            )
            == chunk.text
        )
        assert len(chunk.text) <= 1000


def test_embedding_cache_key_includes_content_model_and_chunk_strategy() -> None:
    base = embedding_cache_key("content", "model-v1", "chunks-v1")
    assert base == embedding_cache_key("content", "model-v1", "chunks-v1")
    assert base != embedding_cache_key("changed-content", "model-v1", "chunks-v1")
    assert base != embedding_cache_key("content", "model-v2", "chunks-v1")
    assert base != embedding_cache_key("content", "model-v1", "chunks-v2")
    assert base != embedding_cache_key("content", "model-v1", "chunks-v1", provider="openai")
    assert base != embedding_cache_key("content", "model-v1", "chunks-v1", dimensions=384)
    assert base != embedding_cache_key(
        "content", "model-v1", "chunks-v1", configuration_hash="normalized"
    )


def test_graph_embedding_property_rejects_untrusted_identifiers() -> None:
    assert GraphStore._embedding_property("a" * 64) == f"embedding_{'a' * 20}"
    with pytest.raises(ValueError, match="SHA-256 digest"):
        GraphStore._embedding_property("a'); MATCH (n) DETACH DELETE n //")


def test_embedding_cache_is_immutable_and_reuses_only_matching_identity(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embeddings")
    stored = cache.put("content", "model-v1", [0.1, 0.2], "chunks-v1")
    assert cache.get("content", "model-v1", "chunks-v1") == stored
    assert cache.put("content", "model-v1", [0.1, 0.2], "chunks-v1") == stored
    assert cache.get("content", "model-v2", "chunks-v1") is None
    assert cache.get("content", "model-v1", "chunks-v1", provider="openai") is None
    with pytest.raises(ValueError, match="different vector"):
        cache.put("content", "model-v1", [0.2, 0.1], "chunks-v1")


def test_markdown_snapshot_chunks_are_deterministic_provenanced_and_identity_sensitive(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    settings = Settings(
        _env_file=None,
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    first = analyze_repository(repo, settings)
    doc_chunks = [item for item in first.retrieval_chunks if item.chunk_kind == "DOCUMENT"]
    assert doc_chunks
    assert doc_chunks == build_document_chunks(
        first.snapshot_id, {"README.md": (repo / "README.md").read_bytes()}
    )
    assert all(item.file_path == "README.md" for item in doc_chunks)
    assert all(item.source_node_id.startswith("docsrc_") for item in doc_chunks)
    assert all(
        item.source_node_id not in {entity.entity_id for entity in first.entities}
        for item in doc_chunks
    )
    assert all(entity.file_path.endswith(".py") for entity in first.entities)
    assert (
        load_snapshot_retrieval_sources(first, settings)["README.md"]
        == (repo / "README.md").read_bytes()
    )

    old_ids = [item.chunk_id for item in doc_chunks]
    (repo / "README.md").write_text("# Payments\n\nChanged payment handling.\n")
    second = analyze_repository(repo, settings)
    assert second.snapshot_id != first.snapshot_id
    assert [
        item.chunk_id for item in second.retrieval_chunks if item.chunk_kind == "DOCUMENT"
    ] != old_ids


def test_document_chunk_boundaries_preserve_utf8_offsets() -> None:
    text = "# Title 🚀\n\n" + ("paragraph café. " * 20) + "\n\nnext section."
    first = build_document_chunks("snapshot", {"docs/guide.md": text.encode()}, max_chunk_chars=80)
    assert first == build_document_chunks(
        "snapshot", {"docs/guide.md": text.encode()}, max_chunk_chars=80
    )
    assert len(first) > 1
    for chunk in first:
        start, end = chunk.source_start_byte, chunk.source_end_byte
        assert len(chunk.text.encode()) == end - start
        assert hashlib.sha256(chunk.text.encode()).hexdigest() == chunk.content_hash


class _CountingProvider:
    def __init__(self, provider: str, model: str, dimensions: int = 2) -> None:
        self.spec = EmbeddingSpec(
            provider=provider,
            model=model,
            dimensions=dimensions,
            configuration=(("normalization", "unit"),),
        )
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int | None]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts], None


def test_unchanged_chunk_hits_cache_and_provider_config_changes_miss(tmp_path: Path) -> None:
    chunks = build_document_chunks("snap", {"guide.md": b"# cache test\n"})
    cache = EmbeddingCache(tmp_path / "cache")
    first_provider = _CountingProvider("local", "model", 2)
    first = embed_chunks(first_provider, chunks, cache)
    second = embed_chunks(first_provider, chunks, cache)
    changed_provider = _CountingProvider("local", "model", 2)
    changed_provider.spec = changed_provider.spec.model_copy(
        update={"configuration": (("normalization", "none"),)}
    )
    third = embed_chunks(changed_provider, chunks, cache)
    assert first_provider.calls == 1
    assert second.cache_hits == 1 and first_provider.calls == 1
    assert changed_provider.calls == 1
    assert third.vectors[0].spec_id != first.vectors[0].spec_id


def test_openai_provider_checks_returned_model_and_normalizes_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    response = SimpleNamespace(
        model="text-embedding-3-small",
        data=[SimpleNamespace(index=0, embedding=[3.0, 4.0, *([0.0] * 1534)])],
        usage=SimpleNamespace(total_tokens=7),
    )

    class FakeOpenAI:
        def __init__(self, *, api_key: str, max_retries: int) -> None:
            assert api_key == "test-only"
            assert max_retries == 0
            self.embeddings = self

        def create(self, **_kwargs: object) -> SimpleNamespace:
            return response

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    budget = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="0.02",
        max_run_cost_usd="0.50",
    )
    provider = OpenAIEmbeddingProvider(api_key="test-only", budget=budget)
    provider._authorize_texts(
        [
            "only the approved benchmark text",
            "do not cache a different model's result",
            "unused preflight capacity " * 100,
        ],
        HostedTransmissionPolicy(
            enabled=True, benchmark_opt_in=True, approved_snapshot_id="snap-approved"
        ),
    )
    vectors, tokens = provider.embed_texts(["only the approved benchmark text"])

    assert vectors[0][:2] == [0.6, 0.8]
    assert len(vectors[0]) == 1536
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)
    assert tokens == 7
    assert dict(provider.spec.configuration)["normalization"] == "l2-float64"

    response.model = "unexpected-model-revision"
    with pytest.raises(RuntimeError, match="expected 'text-embedding-3-small'"):
        provider.embed_texts(["do not cache a different model's result"])


def test_hosted_budget_allows_below_and_exact_boundary_costs() -> None:
    below = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="1.00",
        max_run_cost_usd="0.01",
    )
    below_estimate = below.preflight(["below the configured limit"])
    assert below_estimate.estimated_cost_usd < below_estimate.max_run_cost_usd
    assert below.authorize_request(["below the configured limit"]) > 0

    token_counter = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="1.00",
        max_run_cost_usd="0.01",
    )
    tokens = token_counter.count_tokens(["exact boundary"])
    exact_limit = Decimal(tokens) / Decimal(1_000_000)
    boundary = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="1.00",
        max_run_cost_usd=exact_limit,
    )
    exact = boundary.preflight(["exact boundary"])
    assert exact.estimated_cost_usd == exact.max_run_cost_usd
    assert boundary.authorize_request(["exact boundary"]) == tokens


def test_hosted_budget_rejects_over_limit_before_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class FakeOpenAI:
        calls = 0

        def __init__(self, *, api_key: str, max_retries: int) -> None:
            assert api_key == "test-only"
            assert max_retries == 0
            self.embeddings = self

        def create(self, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            raise AssertionError("provider request must not be invoked")

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    budget = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="1.00",
        max_run_cost_usd="0",
    )
    provider = OpenAIEmbeddingProvider(api_key="test-only", budget=budget)
    with pytest.raises(HostedEmbeddingBudgetError, match="BUDGET_EXCEEDED"):
        provider._authorize_texts(
            ["over budget text"],
            HostedTransmissionPolicy(
                enabled=True, benchmark_opt_in=True, approved_snapshot_id="snap-approved"
            ),
        )
    with pytest.raises(HostedTransmissionError, match="explicit snapshot"):
        provider.embed_texts(["over budget text"])
    assert FakeOpenAI.calls == 0


def test_hosted_budget_fails_closed_for_invalid_pricing_or_tokenizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tiktoken

    with pytest.raises(HostedEmbeddingBudgetError, match="pricing or limit is invalid"):
        HostedEmbeddingBudget(
            model="text-embedding-3-small",
            cost_per_million_tokens_usd="NaN",
            max_run_cost_usd="0.50",
        )
    with pytest.raises(HostedEmbeddingBudgetError, match="pricing or limit is invalid"):
        HostedEmbeddingBudget(
            model="text-embedding-3-small",
            cost_per_million_tokens_usd="0",
            max_run_cost_usd="0.50",
        )
    with pytest.raises(HostedEmbeddingBudgetError, match="No budget pricing"):
        HostedEmbeddingBudget(
            model="unconfigured-model",
            cost_per_million_tokens_usd="0.02",
            max_run_cost_usd="0.50",
        )
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _model: None)
    with pytest.raises(HostedEmbeddingBudgetError, match="token accounting is unavailable"):
        HostedEmbeddingBudget(
            model="text-embedding-3-small",
            cost_per_million_tokens_usd="0.02",
            max_run_cost_usd="0.50",
        )


def test_hosted_budget_rejects_text_outside_preflight_before_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class FakeOpenAI:
        calls = 0

        def __init__(self, *, api_key: str, max_retries: int) -> None:
            assert api_key == "test-only"
            assert max_retries == 0
            self.embeddings = self

        def create(self, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            raise AssertionError("unapproved text must not be transmitted")

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    budget = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="0.02",
        max_run_cost_usd="0.50",
    )
    provider = OpenAIEmbeddingProvider(api_key="test-only", budget=budget)
    provider._authorize_texts(
        ["approved chunk text", "approved query text"],
        HostedTransmissionPolicy(
            enabled=True, benchmark_opt_in=True, approved_snapshot_id="snap-approved"
        ),
    )
    with pytest.raises(HostedEmbeddingBudgetError, match="outside the preflight-approved set"):
        provider.embed_texts(["different text"])
    assert FakeOpenAI.calls == 0


def test_hosted_provider_cannot_transmit_after_budget_preflight_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class FakeOpenAI:
        calls = 0

        def __init__(self, *, api_key: str, max_retries: int) -> None:
            self.embeddings = self

        def create(self, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            raise AssertionError("policy bypass reached the provider")

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    budget = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="0.02",
        max_run_cost_usd="0.50",
    )
    budget.preflight(["repository text"])
    provider = OpenAIEmbeddingProvider(api_key="test-only", budget=budget)

    with pytest.raises(HostedTransmissionError, match="explicit snapshot"):
        provider.embed_texts(["repository text"])
    assert FakeOpenAI.calls == 0


def test_hosted_provider_exposes_no_generic_text_authorization_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    class FakeOpenAI:
        calls = 0

        def __init__(self, *, api_key: str, max_retries: int) -> None:
            self.embeddings = self

        def create(self, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            raise AssertionError("unvalidated repository text reached the provider")

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    budget = HostedEmbeddingBudget(
        model="text-embedding-3-small",
        cost_per_million_tokens_usd="0.02",
        max_run_cost_usd="0.50",
    )
    provider = OpenAIEmbeddingProvider(api_key="test-only", budget=budget)

    assert not hasattr(provider, "authorize_texts")
    with pytest.raises(HostedTransmissionError, match="explicit snapshot"):
        provider.embed_texts(["unvalidated repository text"])
    assert FakeOpenAI.calls == 0


def test_hosted_embedding_requires_opt_in_and_rejects_secret_before_provider_send(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    settings = Settings(
        _env_file=None,
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    chunks = [item for item in analysis.retrieval_chunks if item.chunk_kind == "DOCUMENT"]
    source_bytes = load_snapshot_retrieval_sources(analysis, settings)
    with pytest.raises(HostedTransmissionError, match="disabled"):
        authorize_hosted_chunks(
            analysis,
            chunks,
            source_bytes,
            HostedTransmissionPolicy(approved_snapshot_id=analysis.snapshot_id),
        )
    authorized = HostedTransmissionPolicy(
        enabled=True, benchmark_opt_in=True, approved_snapshot_id=analysis.snapshot_id
    )
    assert authorize_hosted_chunks(analysis, chunks, source_bytes, authorized) == chunks

    secret = "-----BEGIN RSA PRIVATE KEY-----\nmaterial\n-----END RSA PRIVATE KEY-----"
    (repo / "README.md").write_text(secret, encoding="utf-8")
    tainted = analyze_repository(repo, settings)
    tainted_chunks = [item for item in tainted.retrieval_chunks if item.chunk_kind == "DOCUMENT"]
    tainted_bytes = load_snapshot_retrieval_sources(tainted, settings)
    with pytest.raises(HostedTransmissionError, match="Secret scanning rejected"):
        authorize_hosted_chunks(
            tainted,
            tainted_chunks,
            tainted_bytes,
            HostedTransmissionPolicy(
                enabled=True,
                benchmark_opt_in=True,
                approved_snapshot_id=tainted.snapshot_id,
            ),
        )
    with pytest.raises(HostedTransmissionError, match="missing or has changed"):
        authorize_hosted_chunks(analysis, chunks, {"README.md": b"# changed"}, authorized)


def test_hosted_policy_rejects_private_paths_even_inside_manifest(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    settings = Settings(
        _env_file=None,
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin="",
        scip_cli_bin="",
    )
    analysis = analyze_repository(repo, settings)
    chunks = [item for item in analysis.retrieval_chunks if item.chunk_kind == "DOCUMENT"]
    chunk = chunks[0].model_copy(update={"file_path": ".aws/guide.md"})
    manifest = analysis.manifest[0].model_copy(update={"path": ".aws/guide.md"})
    private_analysis = analysis.model_copy(update={"manifest": [manifest]})
    with pytest.raises(HostedTransmissionError, match="source path"):
        authorize_hosted_chunks(
            private_analysis,
            [chunk],
            {".aws/guide.md": chunk.text.encode()},
            HostedTransmissionPolicy(
                enabled=True,
                benchmark_opt_in=True,
                approved_snapshot_id=private_analysis.snapshot_id,
            ),
        )

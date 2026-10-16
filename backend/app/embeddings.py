from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .chunking import EmbeddingCache
from .models import RetrievalChunk, SnapshotAnalysis

LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
LOCAL_MODEL_ONNX_SHA256 = "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"
LOCAL_MAX_SEQUENCE_LENGTH = 256
LOCAL_DIMENSIONS = 384
OPENAI_MODEL = "text-embedding-3-small"
OPENAI_DIMENSIONS = 1536
DEFAULT_EMBEDDING_PROVIDER = "local"
HOSTED_COST_PER_MILLION_TOKENS_USD = Decimal("0.02")
HOSTED_COST_SOURCE_URL = "https://developers.openai.com/api/docs/models/text-embedding-3-small"
HOSTED_COST_SOURCE_RETRIEVED_DATE = "2026-10-03"


class EmbeddingSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    dimensions: int
    normalize: bool = True
    configuration: tuple[tuple[str, str], ...] = ()

    @property
    def identity(self) -> str:
        payload = (
            self.provider,
            self.model,
            self.dimensions,
            self.normalize,
            self.configuration,
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()


class EmbeddedVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    snapshot_id: str
    spec_id: str
    content_hash: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class EmbeddingMeasurement:
    spec: EmbeddingSpec
    vectors: list[EmbeddedVector]
    cache_hits: int
    elapsed_seconds: float
    input_tokens: int | None = None
    estimated_cost_usd: float | None = None


class EmbeddingProvider(Protocol):
    spec: EmbeddingSpec

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int | None]: ...


class LocalMiniLMOnnxProvider:
    def __init__(
        self,
        model_name: str = LOCAL_MODEL,
        *,
        revision: str = LOCAL_MODEL_REVISION,
        device: str = "cpu",
        model_dir: Path | None = None,
    ):
        if model_name != LOCAL_MODEL or revision != LOCAL_MODEL_REVISION:
            raise ValueError(
                "The local M15 baseline is pinned to all-MiniLM-L6-v2 revision 1110a243."
            )
        if device != "cpu":
            raise ValueError(
                "The reproducible local M15 ONNX baseline uses CPUExecutionProvider only."
            )
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer
        from tokenizers import __version__ as tokenizers_version

        if model_dir is None:
            downloaded = snapshot_download(
                repo_id=model_name,
                revision=revision,
                allow_patterns=[
                    "1_Pooling/config.json",
                    "config.json",
                    "modules.json",
                    "onnx/model.onnx",
                    "sentence_bert_config.json",
                    "special_tokens_map.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                ],
            )
            model_dir = Path(downloaded)
        self.model_dir = model_dir.expanduser().resolve(strict=True)
        model_path = self.model_dir / "onnx" / "model.onnx"
        model_sha256 = _sha256_file(model_path)
        if model_sha256 != LOCAL_MODEL_ONNX_SHA256:
            raise ValueError("Pinned all-MiniLM-L6-v2 ONNX artifact failed its SHA-256 check.")
        self._verify_sentence_transformer_config()

        tokenizer_path = self.model_dir / "tokenizer.json"
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer_payload = json.loads(tokenizer_path.read_text(encoding="utf-8"))
        normalizer_config = tokenizer_payload.get("normalizer")
        if (
            not isinstance(normalizer_config, dict)
            or normalizer_config.get("type") != "BertNormalizer"
        ):
            raise ValueError("Pinned MiniLM tokenizer does not use the expected BERT normalizer.")
        if normalizer_config.get("lowercase") is not True:
            raise ValueError(
                "Pinned MiniLM tokenizer JSON must lowercase through its BERT normalizer."
            )
        pad_token_id = self.tokenizer.token_to_id("[PAD]")
        if pad_token_id is None:
            raise ValueError("Pinned MiniLM tokenizer does not define its expected [PAD] token.")
        self.tokenizer.enable_truncation(
            max_length=LOCAL_MAX_SEQUENCE_LENGTH,
            strategy="longest_first",
            stride=0,
            direction="right",
        )
        self.tokenizer.enable_padding(
            direction="right",
            pad_id=pad_token_id,
            pad_type_id=0,
            pad_token="[PAD]",
        )
        self._np = np
        self._ort = ort
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.output_name = self.session.get_outputs()[0].name
        self.artifact_sha256 = self._artifact_identity()
        self.spec = EmbeddingSpec(
            provider="onnxruntime-cpu",
            model=model_name,
            dimensions=LOCAL_DIMENSIONS,
            normalize=True,
            configuration=(
                ("artifact_sha256", self.artifact_sha256),
                ("max_sequence_length", str(LOCAL_MAX_SEQUENCE_LENGTH)),
                ("model_onnx_sha256", model_sha256),
                ("model_revision", revision),
                ("sentence_transformers_do_lower_case", "false"),
                ("tokenizer_normalizer_lowercase", "true"),
                ("onnxruntime", ort.__version__),
                ("onnxruntime_provider", "CPUExecutionProvider"),
                ("pooling", "attention-mask-weighted-mean-including-special-tokens"),
                ("precision", "float32"),
                ("tokenizer_sha256", _sha256_file(tokenizer_path)),
                ("tokenizers", tokenizers_version),
                ("truncation", "longest-first-right-zero-stride"),
                ("normalization", "l2-float32"),
            ),
        )

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int | None]:
        if not texts:
            return [], None
        encodings = self.tokenizer.encode_batch(texts, add_special_tokens=True)
        input_ids = self._np.asarray([item.ids for item in encodings], dtype=self._np.int64)
        attention_mask = self._np.asarray(
            [item.attention_mask for item in encodings], dtype=self._np.int64
        )
        token_type_ids = self._np.asarray(
            [item.type_ids for item in encodings], dtype=self._np.int64
        )
        available_inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }
        outputs = self.session.run(
            [self.output_name],
            {name: value for name, value in available_inputs.items() if name in self.input_names},
        )
        token_embeddings = self._np.asarray(outputs[0], dtype=self._np.float32)
        if token_embeddings.ndim != 3 or token_embeddings.shape[-1] != LOCAL_DIMENSIONS:
            raise ValueError(
                "Pinned MiniLM ONNX model returned an unexpected token embedding shape."
            )
        mask = attention_mask.astype(self._np.float32)[..., None]
        pooled = (token_embeddings * mask).sum(axis=1) / self._np.maximum(mask.sum(axis=1), 1.0)
        norms = self._np.linalg.norm(pooled, axis=1, keepdims=True)
        if self._np.any(norms == 0) or not self._np.isfinite(pooled).all():
            raise ValueError("Pinned MiniLM ONNX model returned an invalid embedding.")
        normalized = pooled / norms
        return [row.astype(float).tolist() for row in normalized], None

    def _verify_sentence_transformer_config(self) -> None:
        model_config = json.loads((self.model_dir / "config.json").read_text(encoding="utf-8"))
        sentence_config = json.loads(
            (self.model_dir / "sentence_bert_config.json").read_text(encoding="utf-8")
        )
        tokenizer_config = json.loads(
            (self.model_dir / "tokenizer_config.json").read_text(encoding="utf-8")
        )
        modules = json.loads((self.model_dir / "modules.json").read_text(encoding="utf-8"))
        pooling = json.loads(
            (self.model_dir / "1_Pooling" / "config.json").read_text(encoding="utf-8")
        )
        if (
            model_config.get("hidden_size") != LOCAL_DIMENSIONS
            or model_config.get("num_hidden_layers") != 6
            or model_config.get("vocab_size") != 30522
            or model_config.get("model_type") != "bert"
        ):
            raise ValueError("Pinned MiniLM transformer configuration is not 384-dimensional.")
        if sentence_config.get("max_seq_length") != LOCAL_MAX_SEQUENCE_LENGTH:
            raise ValueError("Pinned MiniLM sentence-transformer sequence length changed.")
        if sentence_config.get("do_lower_case") is not False:
            raise ValueError("Pinned MiniLM Sentence Transformers casing behavior changed.")
        if tokenizer_config.get("do_lower_case") is not True:
            raise ValueError("Pinned MiniLM tokenizer repository casing configuration changed.")
        module_types = [item.get("type", "").rsplit(".", 1)[-1] for item in modules]
        if module_types != ["Transformer", "Pooling", "Normalize"]:
            raise ValueError(
                "Pinned MiniLM module order does not match Transformer/Pooling/Normalize."
            )
        if pooling.get("word_embedding_dimension") != LOCAL_DIMENSIONS:
            raise ValueError("Pinned MiniLM pooling dimension is not 384.")
        pooling_modes = [
            key for key, value in pooling.items() if key.startswith("pooling_mode_") and value
        ]
        if pooling_modes != ["pooling_mode_mean_tokens"]:
            raise ValueError("Pinned MiniLM must use attention-mask mean pooling only.")

    def _artifact_identity(self) -> str:
        paths = (
            "1_Pooling/config.json",
            "config.json",
            "modules.json",
            "onnx/model.onnx",
            "sentence_bert_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        )
        digest = hashlib.sha256()
        for relative in paths:
            path = self.model_dir / relative
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class HostedTransmissionError(ValueError):
    pass


class HostedEmbeddingBudgetError(ValueError):
    pass


@dataclass(frozen=True)
class HostedEmbeddingBudgetEstimate:
    model: str
    input_tokens: int
    input_text_count: int
    estimated_cost_usd: Decimal
    max_run_cost_usd: Decimal
    cost_per_million_tokens_usd: Decimal
    token_encoding: str
    tokenizer_version: str


class HostedEmbeddingBudget:
    """Fail-closed per-run estimate and request gate for hosted embeddings."""

    def __init__(
        self,
        *,
        model: str,
        cost_per_million_tokens_usd: Decimal | str | float,
        max_run_cost_usd: Decimal | str | float,
    ) -> None:
        if model != OPENAI_MODEL:
            raise HostedEmbeddingBudgetError(
                "No budget pricing/tokenization is configured for this model."
            )
        try:
            price = Decimal(str(cost_per_million_tokens_usd))
            maximum = Decimal(str(max_run_cost_usd))
        except (InvalidOperation, ValueError):
            raise HostedEmbeddingBudgetError(
                "Hosted embedding budget pricing is invalid."
            ) from None
        if not price.is_finite() or price <= 0 or not maximum.is_finite() or maximum < 0:
            raise HostedEmbeddingBudgetError("Hosted embedding budget pricing or limit is invalid.")
        try:
            import tiktoken
            from tiktoken import __version__ as tokenizer_version

            encoding = tiktoken.encoding_for_model(model)
            if encoding.name != "cl100k_base":
                raise HostedEmbeddingBudgetError(
                    "Hosted model token accounting resolved to an unexpected encoding."
                )
        except HostedEmbeddingBudgetError:
            raise
        except Exception:
            raise HostedEmbeddingBudgetError(
                "Hosted embedding budget token accounting is unavailable for the configured model."
            ) from None
        self.model = model
        self.cost_per_million_tokens_usd = price
        self.max_run_cost_usd = maximum
        self.token_encoding = encoding.name
        self.tokenizer_version = tokenizer_version
        self._encoding = encoding
        self._estimate: HostedEmbeddingBudgetEstimate | None = None
        self._allowed_texts: Counter[str] = Counter()
        self._reserved_tokens = 0
        self._actual_tokens = 0

    def count_tokens(self, texts: list[str]) -> int:
        if any(not isinstance(text, str) for text in texts):
            raise HostedEmbeddingBudgetError("Hosted embedding token input is invalid.")
        try:
            return sum(len(self._encoding.encode(text, disallowed_special=())) for text in texts)
        except Exception:
            raise HostedEmbeddingBudgetError(
                "Hosted embedding budget token estimation failed."
            ) from None

    def _cost(self, tokens: int) -> Decimal:
        return Decimal(tokens) * self.cost_per_million_tokens_usd / Decimal(1_000_000)

    @staticmethod
    def _text_key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "strict")).hexdigest()

    def preflight(self, texts: list[str]) -> HostedEmbeddingBudgetEstimate:
        if self._estimate is not None:
            raise HostedEmbeddingBudgetError(
                "Hosted embedding budget has already been authorized for this run."
            )
        if not texts:
            raise HostedEmbeddingBudgetError("Hosted embedding budget has no selected input text.")
        tokens = self.count_tokens(texts)
        estimated_cost = self._cost(tokens)
        if estimated_cost > self.max_run_cost_usd:
            raise HostedEmbeddingBudgetError(
                "BUDGET_EXCEEDED: estimated hosted embedding cost exceeds the configured run limit."
            )
        self._allowed_texts = Counter(self._text_key(text) for text in texts)
        self._reserved_tokens = 0
        self._actual_tokens = 0
        self._estimate = HostedEmbeddingBudgetEstimate(
            model=self.model,
            input_tokens=tokens,
            input_text_count=len(texts),
            estimated_cost_usd=estimated_cost,
            max_run_cost_usd=self.max_run_cost_usd,
            cost_per_million_tokens_usd=self.cost_per_million_tokens_usd,
            token_encoding=self.token_encoding,
            tokenizer_version=self.tokenizer_version,
        )
        return self._estimate

    def authorize_request(self, texts: list[str]) -> int:
        if self._estimate is None:
            raise HostedEmbeddingBudgetError(
                "BUDGET_EXCEEDED: hosted request has no successful preflight authorization."
            )
        requested = Counter(self._text_key(text) for text in texts)
        if not texts or any(count > self._allowed_texts[key] for key, count in requested.items()):
            raise HostedEmbeddingBudgetError(
                "BUDGET_EXCEEDED: hosted request text is outside the preflight-approved set."
            )
        tokens = self.count_tokens(texts)
        projected_tokens = max(self._reserved_tokens, self._actual_tokens) + tokens
        if (
            projected_tokens > self._estimate.input_tokens
            or self._cost(projected_tokens) > self.max_run_cost_usd
        ):
            raise HostedEmbeddingBudgetError(
                "BUDGET_EXCEEDED: hosted request would exceed the preflight run budget."
            )
        self._allowed_texts.subtract(requested)
        self._reserved_tokens += tokens
        return tokens

    def record_actual_usage(self, tokens: int) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise HostedEmbeddingBudgetError("Provider returned invalid actual token usage.")
        self._actual_tokens += tokens

    def report(self) -> dict[str, str | int | bool | None]:
        estimate = self._estimate
        return {
            "model": self.model,
            "estimated_input_tokens": estimate.input_tokens if estimate else None,
            "preflight_input_text_count": estimate.input_text_count if estimate else None,
            "estimated_cost_usd": str(estimate.estimated_cost_usd) if estimate else None,
            "actual_input_tokens": self._actual_tokens,
            "actual_cost_usd": str(self._cost(self._actual_tokens)),
            "max_run_cost_usd": str(self.max_run_cost_usd),
            "cost_per_million_tokens_usd": str(self.cost_per_million_tokens_usd),
            "token_encoding": self.token_encoding,
            "tokenizer_version": self.tokenizer_version,
            "actual_cost_exceeded_limit": self._cost(self._actual_tokens) > self.max_run_cost_usd,
        }


class HostedTransmissionPolicy(BaseModel):
    """Opt-in controls for the specifically approved M15 benchmark only."""

    enabled: bool = False
    benchmark_opt_in: bool = False
    approved_snapshot_id: str | None = None


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?im)^\s*(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)https?://[^\s/:]+:[^\s/@]+@"),
)
_PRIVATE_PATH_PARTS = {
    ".env",
    ".aws",
    ".ssh",
    "credentials",
    "secrets",
    "private",
}
_PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def _secret_findings(text: str) -> list[str]:
    findings = [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(text)]
    for line in text.splitlines():
        stripped = line.strip().strip("'\"")
        if "=" not in stripped or stripped.startswith(("#", "//")):
            continue
        name, value = stripped.split("=", 1)
        if re.search(r"(?i)(secret|token|password|credential|api[_-]?key)", name):
            continue
        token = value.strip().strip("'\"")
        if len(token) >= 32 and len(set(token)) >= 12 and re.fullmatch(r"[A-Za-z0-9_+/=-]+", token):
            findings.append("high-entropy assignment")
            break
    return findings


def _project_secret_scanner_findings(text: str) -> list[str]:
    """Run the standard offline detect-secrets plugin suite on the selected text."""
    try:
        from detect_secrets import SecretsCollection
        from detect_secrets.settings import default_settings
    except ImportError as exc:
        raise HostedTransmissionError(
            "Hosted embedding is unavailable because the detect-secrets scanner is not installed."
        ) from exc
    with tempfile.TemporaryDirectory(prefix="codegraph-secret-scan-") as folder:
        directory = Path(folder)
        directory.chmod(0o700)
        candidate = directory / "selected-chunk.txt"
        candidate.write_text(text, encoding="utf-8")
        candidate.chmod(0o600)
        found = SecretsCollection()
        with default_settings():
            found.scan_file(str(candidate))
        return [str(secret.type) for _, secret in found]


def authorize_hosted_chunks(
    analysis: SnapshotAnalysis,
    chunks: list[RetrievalChunk],
    source_bytes: dict[str, bytes],
    policy: HostedTransmissionPolicy,
) -> list[RetrievalChunk]:
    """Prove snapshot scope and scan every exact chunk before hosted transmission."""
    if not policy.enabled or not policy.benchmark_opt_in:
        raise HostedTransmissionError(
            "Hosted embedding is disabled; explicit benchmark opt-in and "
            "HOSTED_EMBEDDING_ENABLED=true are both required."
        )
    if policy.approved_snapshot_id != analysis.snapshot_id:
        raise HostedTransmissionError("Hosted benchmark policy does not approve this snapshot.")
    manifest = {entry.path: entry for entry in analysis.manifest}
    approved: list[RetrievalChunk] = []
    for chunk in chunks:
        if chunk.snapshot_id != analysis.snapshot_id:
            raise HostedTransmissionError("Chunk belongs to a different snapshot.")
        entry = manifest.get(chunk.file_path)
        if entry is None or not entry.analyzed:
            raise HostedTransmissionError("Chunk source is outside the approved snapshot set.")
        relative = Path(chunk.file_path)
        parts = {part.lower() for part in relative.parts}
        if (
            relative.is_absolute()
            or ".git" in parts
            or parts & _PRIVATE_PATH_PARTS
            or relative.suffix.lower() in _PRIVATE_SUFFIXES
            or relative.name.lower().startswith((".env", "id_rsa", "id_ed25519"))
        ):
            raise HostedTransmissionError(
                f"Transmission policy rejects source path: {chunk.file_path}"
            )
        content = source_bytes.get(chunk.file_path)
        if content is None or hashlib.sha256(content).hexdigest() != entry.content_hash:
            raise HostedTransmissionError("Snapshot source content is missing or has changed.")
        if not 0 <= chunk.source_start_byte < chunk.source_end_byte <= len(content):
            raise HostedTransmissionError("Chunk byte span is outside the approved source file.")
        exact_text = content[chunk.source_start_byte : chunk.source_end_byte].decode(
            "utf-8", "strict"
        )
        if (
            exact_text != chunk.text
            or hashlib.sha256(exact_text.encode("utf-8")).hexdigest() != chunk.content_hash
        ):
            raise HostedTransmissionError("Chunk content does not match its immutable source span.")
        if _secret_findings(chunk.text) or _project_secret_scanner_findings(chunk.text):
            raise HostedTransmissionError(
                f"Secret scanning rejected selected content from {chunk.file_path}."
            )
        approved.append(chunk)
    return approved


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        dimensions: int = OPENAI_DIMENSIONS,
        budget: HostedEmbeddingBudget,
    ):
        if dimensions != OPENAI_DIMENSIONS:
            raise ValueError("M15 baseline uses text-embedding-3-small default dimensions (1536).")
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required for the hosted M15 benchmark.")
        from openai import OpenAI
        from openai import __version__ as openai_version

        self.client = OpenAI(api_key=key, max_retries=0)
        self.budget = budget
        self._transmission_authorized = False
        self.spec = EmbeddingSpec(
            provider="openai",
            model=OPENAI_MODEL,
            dimensions=dimensions,
            normalize=True,
            configuration=(
                ("encoding_format", "float"),
                ("normalization", "l2-float64"),
                ("openai_sdk", openai_version),
                ("token_accounting_encoding", budget.token_encoding),
                ("token_accounting_tiktoken", budget.tokenizer_version),
            ),
        )

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int | None]:
        if not self._transmission_authorized:
            raise HostedTransmissionError(
                "Hosted provider requires explicit snapshot transmission-policy authorization."
            )
        self.budget.authorize_request(texts)
        response = self.client.embeddings.create(
            model=self.spec.model,
            input=texts,
            dimensions=self.spec.dimensions,
            encoding_format="float",
        )
        self.budget.record_actual_usage(response.usage.total_tokens)
        if response.model != self.spec.model:
            raise RuntimeError(
                f"OpenAI returned model {response.model!r}; expected {self.spec.model!r}."
            )
        vectors: list[list[float]] = []
        for item in sorted(response.data, key=lambda x: x.index):
            vector = [float(value) for value in item.embedding]
            if len(vector) != self.spec.dimensions or not all(
                math.isfinite(value) for value in vector
            ):
                raise ValueError("OpenAI returned an invalid embedding vector.")
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0:
                raise ValueError("OpenAI returned a zero embedding vector.")
            vectors.append([value / norm for value in vector])
        return vectors, response.usage.total_tokens

    def embed_approved_chunks(
        self,
        analysis: SnapshotAnalysis,
        chunks: list[RetrievalChunk],
        source_bytes: dict[str, bytes],
        policy: HostedTransmissionPolicy,
        cache: EmbeddingCache,
    ) -> EmbeddingMeasurement:
        approved = authorize_hosted_chunks(analysis, chunks, source_bytes, policy)
        self._authorize_texts([chunk.text for chunk in approved], policy)
        return embed_chunks(self, approved, cache)

    def _authorize_texts(self, texts: list[str], policy: HostedTransmissionPolicy) -> None:
        if not policy.enabled or not policy.benchmark_opt_in or not policy.approved_snapshot_id:
            raise HostedTransmissionError(
                "Hosted embedding requires explicit opt-in and a bound snapshot policy."
            )
        if not texts:
            raise HostedTransmissionError("Hosted embedding has no approved text to transmit.")
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text:
                raise HostedTransmissionError("Hosted embedding text is invalid.")
            if _secret_findings(text) or _project_secret_scanner_findings(text):
                raise HostedTransmissionError(
                    f"Secret scanning rejected selected text {index + 1}."
                )
        self.budget.preflight(texts)
        self._transmission_authorized = True

    def authorize_benchmark_input(
        self,
        analysis: SnapshotAnalysis,
        chunks: list[RetrievalChunk],
        source_bytes: dict[str, bytes],
        queries: list[str],
        policy: HostedTransmissionPolicy,
    ) -> list[RetrievalChunk]:
        """Authorize and preflight the complete exact benchmark transmission set."""
        approved = authorize_hosted_chunks(analysis, chunks, source_bytes, policy)
        self._authorize_texts([chunk.text for chunk in approved] + queries, policy)
        return approved


def embed_chunks(
    provider: EmbeddingProvider,
    chunks: list[RetrievalChunk],
    cache: EmbeddingCache,
    *,
    batch_size: int = 64,
) -> EmbeddingMeasurement:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    started = time.perf_counter()
    vectors: dict[str, EmbeddedVector] = {}
    missing: list[RetrievalChunk] = []
    hits = 0
    for chunk in chunks:
        cached = cache.get(
            chunk.content_hash,
            provider.spec.model,
            chunk.chunk_strategy_version,
            provider=provider.spec.provider,
            dimensions=provider.spec.dimensions,
            configuration_hash=provider.spec.identity,
        )
        if cached is None:
            missing.append(chunk)
            continue
        hits += 1
        vectors[chunk.chunk_id] = EmbeddedVector(
            chunk_id=chunk.chunk_id,
            snapshot_id=chunk.snapshot_id,
            spec_id=provider.spec.identity,
            content_hash=chunk.content_hash,
            vector=tuple(cached.vector),
        )
    total_tokens = 0
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset : offset + batch_size]
        encoded, tokens = provider.embed_texts([item.text for item in batch])
        if len(encoded) != len(batch):
            raise ValueError("Embedding provider returned the wrong number of vectors.")
        total_tokens += tokens or 0
        for chunk, vector in zip(batch, encoded, strict=True):
            if len(vector) != provider.spec.dimensions:
                raise ValueError("Embedding provider returned an unexpected vector dimension.")
            cached = cache.put(
                chunk.content_hash,
                provider.spec.model,
                vector,
                chunk.chunk_strategy_version,
                provider=provider.spec.provider,
                dimensions=provider.spec.dimensions,
                configuration_hash=provider.spec.identity,
            )
            vectors[chunk.chunk_id] = EmbeddedVector(
                chunk_id=chunk.chunk_id,
                snapshot_id=chunk.snapshot_id,
                spec_id=provider.spec.identity,
                content_hash=chunk.content_hash,
                vector=tuple(cached.vector),
            )
    elapsed = time.perf_counter() - started
    hosted_budget = provider.budget if isinstance(provider, OpenAIEmbeddingProvider) else None
    return EmbeddingMeasurement(
        spec=provider.spec,
        vectors=[vectors[item.chunk_id] for item in chunks],
        cache_hits=hits,
        elapsed_seconds=elapsed,
        input_tokens=total_tokens if provider.spec.provider == "openai" else None,
        estimated_cost_usd=(
            float(
                Decimal(total_tokens)
                * provider.budget.cost_per_million_tokens_usd  # type: ignore[attr-defined]
                / Decimal(1_000_000)
            )
            if hosted_budget is not None
            else None
        ),
    )

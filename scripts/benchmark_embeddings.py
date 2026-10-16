from __future__ import annotations

import argparse
import json
import math
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.chunking import EmbeddingCache
from backend.app.config import Settings
from backend.app.embeddings import (
    DEFAULT_EMBEDDING_PROVIDER,
    HOSTED_COST_SOURCE_RETRIEVED_DATE,
    HOSTED_COST_SOURCE_URL,
    HostedEmbeddingBudget,
    HostedTransmissionError,
    HostedTransmissionPolicy,
    LocalMiniLMOnnxProvider,
    OpenAIEmbeddingProvider,
    embed_chunks,
)
from backend.app.ingestion import analyze_repository
from backend.app.models import RetrievalChunk


def _cosine(left: tuple[float, ...], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Query and document embedding dimensions differ.")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _analyze_with_configured_roots(root: Path):
    """Inspect only under the operator-configured repository allowlist."""
    return analyze_repository(root, Settings())


def _quality(
    queries: list[dict[str, Any]],
    query_vectors: list[list[float]],
    chunk_vectors: dict[str, tuple[float, ...]],
    chunks: list[RetrievalChunk],
    cutoff: int,
) -> dict[str, float]:
    chunks_by_id = {item.chunk_id: item for item in chunks}
    reciprocal_ranks: list[float] = []
    recalls_at_one: list[float] = []
    recalls_at_five: list[float] = []
    precisions_at_five: list[float] = []
    ndcgs_at_ten: list[float] = []
    for item, query in zip(queries, query_vectors, strict=True):
        ranked = sorted(
            chunks,
            key=lambda chunk: (-_cosine(chunk_vectors[chunk.chunk_id], query), chunk.chunk_id),
        )
        relevant = set(item["relevant_paths"])
        relevant_ranks = [
            rank for rank, chunk in enumerate(ranked, 1) if chunk.file_path in relevant
        ]
        reciprocal_ranks.append(1.0 / relevant_ranks[0] if relevant_ranks else 0.0)
        recalls_at_one.append(
            float(bool(relevant_ranks and ranked[0].file_path in relevant)) / len(relevant)
        )
        top_five = ranked[: min(5, cutoff)]
        recalls_at_five.append(
            len({chunk.file_path for chunk in top_five if chunk.file_path in relevant})
            / len(relevant)
        )
        precisions_at_five.append(sum(chunk.file_path in relevant for chunk in top_five) / 5)
        gains = [
            1.0 / math.log2(rank + 1)
            for rank, chunk in enumerate(ranked[:10], 1)
            if chunk.file_path in relevant
        ]
        ideal_relevant = min(
            10, sum(chunk.file_path in relevant for chunk in chunks_by_id.values())
        )
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_relevant + 1))
        ndcgs_at_ten.append(sum(gains) / ideal if ideal else 0.0)
    count = len(queries)
    return {
        "Recall@1": sum(recalls_at_one) / count,
        "Recall@5": sum(recalls_at_five) / count,
        "Precision@5": sum(precisions_at_five) / count,
        "MRR": sum(reciprocal_ranks) / count,
        "nDCG@10": sum(ndcgs_at_ten) / count,
    }


def _run_provider(
    provider_name: str,
    provider: Any,
    queries: list[dict[str, Any]],
    chunks: list[RetrievalChunk],
    cache: EmbeddingCache,
    cutoff: int,
) -> dict[str, Any]:
    process = psutil.Process()
    rss_before = process.memory_info().rss
    chunk_measurement = embed_chunks(provider, chunks, cache)
    query_started = time.perf_counter()
    query_vectors, query_tokens = provider.embed_texts([query["query"] for query in queries])
    if len(query_vectors) != len(queries):
        raise ValueError("Embedding provider returned the wrong number of query vectors.")
    query_elapsed = time.perf_counter() - query_started
    rank_started = time.perf_counter()
    vector_by_chunk = {item.chunk_id: item.vector for item in chunk_measurement.vectors}
    quality = _quality(queries, query_vectors, vector_by_chunk, chunks, cutoff)
    retrieval_elapsed = time.perf_counter() - rank_started
    token_count = (chunk_measurement.input_tokens or 0) + (query_tokens or 0)
    budget_report = provider.budget.report() if provider.spec.provider == "openai" else None
    estimated_cost = (
        float(budget_report["estimated_cost_usd"] or 0) if budget_report is not None else 0.0
    )
    actual_cost = float(budget_report["actual_cost_usd"] or 0) if budget_report is not None else 0.0
    max_rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_after = process.memory_info().rss
    max_rss_scale = 1 if sys.platform == "darwin" else 1024
    return {
        "provider": provider.spec.provider,
        "model": provider.spec.model,
        "model_configuration_id": provider.spec.identity,
        "provider_configuration": dict(provider.spec.configuration),
        "dimensions": provider.spec.dimensions,
        "chunk_count": len(chunks),
        "vector_payload_bytes": len(chunks) * provider.spec.dimensions * 4,
        "vector_index_memory_estimate_bytes": math.ceil(
            len(chunks) * provider.spec.dimensions * 4 * 1.2
        ),
        "vector_index_memory_estimate_method": (
            "float32 vector payload plus 20% HNSW overhead estimate; excludes graph metadata"
        ),
        "cache_hits": chunk_measurement.cache_hits,
        "quality": quality,
        "chunk_embedding_seconds": chunk_measurement.elapsed_seconds,
        "chunks_per_second": (
            len(chunks) / chunk_measurement.elapsed_seconds
            if chunk_measurement.elapsed_seconds > 0
            else None
        ),
        "query_embedding_seconds": query_elapsed,
        "retrieval_ranking_seconds": retrieval_elapsed,
        "peak_rss_bytes": max_rss_after * max_rss_scale,
        "rss_delta_bytes": max(0, rss_after - rss_before),
        "input_tokens": token_count if provider.spec.provider == "openai" else None,
        "estimated_cost_usd": estimated_cost,
        "actual_cost_usd": actual_cost,
        "hosted_budget_preflight": budget_report,
    }


def _run_both_isolated(args: argparse.Namespace) -> int:
    """Measure each provider in a fresh process so peak memory is comparable."""
    reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="codegraph-m15-benchmark-") as temporary:
        temporary_root = Path(temporary)
        for provider_name in ("local", "hosted"):
            report_path = temporary_root / f"{provider_name}.json"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                str(args.repository),
                "--provider",
                provider_name,
                "--evaluation-set",
                str(args.evaluation_set),
                "--output",
                str(report_path),
            ]
            if args.benchmark_hosted:
                command.append("--benchmark-hosted")
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode:
                sys.stderr.write(completed.stderr)
                return completed.returncode
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
    local_report, hosted_report = reports
    if (
        local_report["snapshot_id"] != hosted_report["snapshot_id"]
        or local_report["snapshot_source_manifest_hash"]
        != hosted_report["snapshot_source_manifest_hash"]
        or local_report["selected_paths"] != hosted_report["selected_paths"]
    ):
        raise RuntimeError("Provider benchmarks did not use the same immutable snapshot set.")
    report = {
        **local_report,
        "hosted_transmission_opt_in": args.benchmark_hosted,
        "results": local_report["results"] + hosted_report["results"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M15 code/document retrieval baseline")
    parser.add_argument("repository", type=Path)
    parser.add_argument(
        "--provider",
        choices=("local", "hosted", "both"),
        default=DEFAULT_EMBEDDING_PROVIDER,
    )
    parser.add_argument(
        "--benchmark-hosted",
        action="store_true",
        help="Explicitly opt in to the user-approved, secret-scanned M15 hosted benchmark.",
    )
    parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=Path("docs/evaluation/retrieval-m15.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evaluation/reports/m15-embeddings.json"),
    )
    args = parser.parse_args()
    if args.provider == "both":
        return _run_both_isolated(args)
    root = args.repository.expanduser().resolve(strict=True)
    # Authorization must come from the operator's configured allowlist.  The
    # positional CLI argument is an untrusted request, never an authority.
    setting = Settings()
    analysis = _analyze_with_configured_roots(root)
    evaluation = json.loads(args.evaluation_set.read_text(encoding="utf-8"))
    queries = evaluation["queries"]
    selected_paths = set(evaluation["candidate_paths"])
    chunks = [chunk for chunk in analysis.retrieval_chunks if chunk.file_path in selected_paths]
    if not chunks:
        raise RuntimeError("The retrieval evaluation set selected no snapshot chunks.")
    source_bytes = __import__(
        "backend.app.chunking", fromlist=["load_snapshot_retrieval_sources"]
    ).load_snapshot_retrieval_sources(analysis, setting)
    requested = ["local", "hosted"] if args.provider == "both" else [args.provider]
    results: list[dict[str, Any]] = []
    cache = EmbeddingCache(setting.embedding_cache_dir)
    if "hosted" in requested:
        if not args.benchmark_hosted or not setting.hosted_embedding_enabled:
            raise HostedTransmissionError(
                "Hosted benchmark requires --benchmark-hosted and "
                "HOSTED_EMBEDDING_ENABLED=true; API-key presence alone never enables it."
            )
        policy = HostedTransmissionPolicy(
            enabled=setting.hosted_embedding_enabled,
            benchmark_opt_in=args.benchmark_hosted,
            approved_snapshot_id=analysis.snapshot_id,
        )
    if "local" in requested:
        local = LocalMiniLMOnnxProvider(device="cpu")
        results.append(_run_provider("local", local, queries, chunks, cache, evaluation["cutoff"]))
    if "hosted" in requested:
        hosted_api_key = (
            setting.openai_api_key.get_secret_value() if setting.openai_api_key else None
        )
        hosted = OpenAIEmbeddingProvider(
            api_key=hosted_api_key,
            budget=HostedEmbeddingBudget(
                model="text-embedding-3-small",
                cost_per_million_tokens_usd=(setting.hosted_embedding_cost_per_million_tokens_usd),
                max_run_cost_usd=setting.max_hosted_embedding_run_cost_usd,
            ),
        )
        chunks = hosted.authorize_benchmark_input(
            analysis,
            chunks,
            source_bytes,
            [query["query"] for query in queries],
            policy,
        )
        results.append(
            _run_provider("hosted", hosted, queries, chunks, cache, evaluation["cutoff"])
        )
    report = {
        "dataset_id": evaluation["dataset_id"],
        "measurement_environment": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "snapshot_id": analysis.snapshot_id,
        "snapshot_source_manifest_hash": analysis.analysis_environment.source_manifest_hash
        if analysis.analysis_environment
        else None,
        "evaluation_queries": len(queries),
        "selected_chunk_count": len(chunks),
        "selected_paths": sorted(selected_paths),
        "quality_ranking_method": "exact cosine dot-product ranking across selected chunks",
        "secret_scanner": "detect-secrets 1.5.0 + fail-closed credential/path policy",
        "hosted_transmission_opt_in": args.benchmark_hosted,
        "hosted_cost_usd_per_million_tokens": float(
            setting.hosted_embedding_cost_per_million_tokens_usd
        ),
        "hosted_cost_source": {
            "url": HOSTED_COST_SOURCE_URL,
            "retrieved_date": HOSTED_COST_SOURCE_RETRIEVED_DATE,
        },
        "hosted_budget_limit_usd": str(setting.max_hosted_embedding_run_cost_usd),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

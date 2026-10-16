from __future__ import annotations

import argparse
import json
import math
import os
import resource
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import psutil
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.chunking import EmbeddingCache
from backend.app.config import Settings
from backend.app.embeddings import LocalMiniLMOnnxProvider, embed_chunks
from backend.app.graph import GraphStore
from backend.app.ingestion import analyze_repository
from backend.app.models import SnapshotStatus
from backend.app.schema import validate_analysis

# ruff: noqa: E501  # Keep report scope descriptions intact.


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def command_text(*args: str) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the M26 pre-optimization demo pipeline.")
    parser.add_argument("repository", type=Path)
    parser.add_argument("--graph-port", type=int, default=16379)
    parser.add_argument("--container", default="codex-m26-falkordb")
    parser.add_argument("--output", type=Path, default=Path("reports/performance-baseline.json"))
    parser.add_argument("--api-samples", type=int, default=30)
    args = parser.parse_args()
    root = args.repository.expanduser().resolve(strict=True)
    os.environ["ALLOWED_REPOSITORY_ROOTS"] = str(root)
    os.environ["FALKORDB_HOST"] = "127.0.0.1"
    os.environ["FALKORDB_PORT"] = str(args.graph_port)
    settings = Settings(
        allowed_repository_roots=[root],
        falkordb_host="127.0.0.1",
        falkordb_port=args.graph_port,
    )

    process = psutil.Process()
    stage_durations: dict[str, float] = {}
    stage_started: float | None = None
    current_stage: str | None = None

    def on_stage(stage: str) -> None:
        nonlocal stage_started, current_stage
        now = time.perf_counter()
        if current_stage is not None and stage_started is not None:
            stage_durations[current_stage] = now - stage_started
        current_stage = stage
        stage_started = now

    rss_before = process.memory_info().rss
    analysis_started = time.perf_counter()
    analysis = analyze_repository(root, settings, on_stage=on_stage)
    analysis_seconds = time.perf_counter() - analysis_started
    if current_stage is not None and stage_started is not None:
        stage_durations[current_stage] = time.perf_counter() - stage_started
    validate_analysis(analysis)
    if analysis.semantic_indexing != "INDEXED":
        raise RuntimeError(f"semantic index did not complete: {analysis.semantic_indexing}")

    store = GraphStore(settings)
    if not store.ping():
        raise RuntimeError("isolated FalkorDB did not answer PING")
    store.register_repository(analysis.repository)
    graph_started = time.perf_counter()
    store.publish_snapshot(analysis)
    graph_load_seconds = time.perf_counter() - graph_started
    if store.snapshot_status(analysis.snapshot_id) != SnapshotStatus.READY.value:
        raise RuntimeError("benchmark snapshot did not reach READY")

    provider = LocalMiniLMOnnxProvider(device="cpu")
    vector_started = time.perf_counter()
    measurement = embed_chunks(
        provider,
        analysis.retrieval_chunks,
        EmbeddingCache(settings.embedding_cache_dir),
    )
    vector_encode_seconds = time.perf_counter() - vector_started
    index_started = time.perf_counter()
    store.store_chunk_embeddings(analysis.snapshot_id, measurement)
    vector_persist_seconds = time.perf_counter() - index_started

    target_counts = Counter(
        binding.target_entity_id
        for binding in analysis.bindings
        if binding.target_entity_id is not None
    )
    target_id = target_counts.most_common(1)[0][0] if target_counts else None
    if target_id is None:
        raise RuntimeError("snapshot has no exact binding target for caller-query benchmark")

    # Exercise the real FastAPI endpoint; each request reloads and validates the READY
    # snapshot context as in production. TestClient uses in-process ASGI, no network hop.
    from backend.app.main import app

    latencies_ms: list[float] = []
    statuses: Counter[str] = Counter()
    with TestClient(app) as client:
        query = urlencode({"target_id": target_id, "limit": 100})
        endpoint = f"/api/snapshots/{analysis.snapshot_id}/graph/callers?{query}"
        for _ in range(args.api_samples):
            started = time.perf_counter()
            response = client.get(endpoint)
            latencies_ms.append((time.perf_counter() - started) * 1000)
            statuses[str(response.status_code)] += 1
            if response.status_code != 200:
                raise RuntimeError(f"graph callers API returned {response.status_code}")

    peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        peak_rss *= 1024
    current_rss = process.memory_info().rss
    docker_info = command_text("docker", "info", "--format", "{{.MemTotal}}")
    docker_stats = command_text(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.MemUsage}}\t{{.CPUPerc}}",
        args.container,
    )
    stats_parts = docker_stats.split("\t") if docker_stats else []
    docker_memory_used = stats_parts[0] if stats_parts else None
    docker_cpu_percent = stats_parts[1] if len(stats_parts) > 1 else None
    swap = command_text("sysctl", "-n", "vm.swapusage")
    pressure = command_text("memory_pressure", "-Q")

    report: dict[str, Any] = {
        "benchmark": "M26 pre-optimization baseline",
        "repository": {
            "path": str(root),
            "head_commit": analysis.repository.head_commit,
            "snapshot_id": analysis.snapshot_id,
            "is_dirty": analysis.repository.is_dirty,
        },
        "host": {
            "model_identifier": "MacBookPro15,1",
            "processor": "Intel Core i7-8850H, 6 cores",
            "memory_bytes": 16 * 1024**3,
            "platform": "macOS 15.7.9 x86_64",
            "docker_vm_memory_bytes": int(docker_info)
            if docker_info and docker_info.isdigit()
            else None,
            "swap_usage": swap,
            "memory_pressure": pressure,
        },
        "pipeline": {
            "analysis_total_seconds": analysis_seconds,
            "materialization_stage_seconds": stage_durations.get("MATERIALIZING"),
            "parse_stage_seconds": stage_durations.get("PARSING"),
            "semantic_index_and_join_seconds": stage_durations.get("SEMANTIC_INDEXING"),
            "validation_and_cache_write_seconds": stage_durations.get("VALIDATING"),
            "graph_load_seconds": graph_load_seconds,
            "vector_encode_seconds": measurement.elapsed_seconds,
            "vector_encode_wrapper_seconds": vector_encode_seconds,
            "vector_chunk_count": len(measurement.vectors),
            "vector_cache_hits": measurement.cache_hits,
            "vector_persist_and_index_seconds": vector_persist_seconds,
            "query": {
                "endpoint": "GET /api/snapshots/{snapshot_id}/graph/callers",
                "samples": len(latencies_ms),
                "status_counts": dict(statuses),
                "p50_ms": percentile(latencies_ms, 0.50),
                "p95_ms": percentile(latencies_ms, 0.95),
                "scope": "In-process FastAPI TestClient; includes snapshot-context load, READY guard, graph-tool construction, query, and JSON serialization; excludes network/browser latency.",
            },
        },
        "resources": {
            "backend_process_rss_before_bytes": rss_before,
            "backend_process_rss_after_bytes": current_rss,
            "backend_process_peak_rss_bytes": peak_rss,
            "falkordb_container_memory_usage": docker_memory_used,
            "falkordb_container_cpu_percent": docker_cpu_percent,
            "docker_total_ram_bytes": int(docker_info)
            if docker_info and docker_info.isdigit()
            else None,
            "docker_containers_in_measurement": [args.container],
        },
        "frontend_graph_render": {
            "status": "PENDING_BROWSER_MEASUREMENT",
            "method": "Measure actual Graph Explorer render with browser performance marks and DOM rows on the same READY snapshot.",
        },
        "correctness": {
            "semantic_indexing": analysis.semantic_indexing,
            "schema_validation": "PASS",
            "snapshot_status": store.snapshot_status(analysis.snapshot_id),
            "entity_count": len(analysis.entities),
            "site_count": len(analysis.sites),
            "provenance_count": len(analysis.provenance),
            "retrieval_chunk_count": len(analysis.retrieval_chunks),
        },
        "measurement_limits": [
            "This report is a cold ingestion and first-time local-vector baseline for the frozen repository. It does not claim performance on other repositories.",
            "The default long-running Compose service and its persistent volume are not used or modified; FalkorDB runs in the separate bounded ephemeral benchmark container.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

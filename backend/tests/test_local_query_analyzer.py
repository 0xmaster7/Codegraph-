from __future__ import annotations

import asyncio
import io
import json
import os

import pytest
from pydantic import ValidationError

from backend.app import main
from backend.app.config import Settings
from backend.app.local_query_analyzer import OllamaQueryAnalyzerProvider
from backend.app.main import QueryExecutionRequest
from backend.app.orchestrator import LLMProviderCompletion, LLMQueryAnalyzer
from backend.app.planner import GraphOperation, Route
from backend.tests.test_graph_tools import SNAPSHOT, _analysis
from backend.tests.test_orchestrator import _tools


def test_query_analyzer_configuration_rejects_non_loopback_providers() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(local_query_analyzer_url="https://provider.example/v1")


@pytest.mark.skipif(
    os.environ.get("CODEGRAPH_LOCAL_LLM_INTEGRATION") != "1",
    reason="requires the approved local Ollama model service",
)
def test_production_query_route_uses_local_model_and_keeps_graph_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyStore:
        def snapshot_status(self, _snapshot_id: str) -> str:
            return "READY"

    class ToolFactory:
        @staticmethod
        def from_store(_analysis: object, _store: object) -> object:
            return _tools()

    settings = Settings(
        local_query_analyzer_enabled=True,
        local_query_analyzer_url="http://127.0.0.1:11434",
        local_query_analyzer_model="qwen2.5:0.5b",
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "GraphStore", lambda _settings: ReadOnlyStore())
    monkeypatch.setattr(
        main,
        "load_snapshot_analysis",
        lambda _snapshot, _settings: _analysis().model_copy(update={"status": "READY"}),
    )
    monkeypatch.setattr(main, "DeterministicGraphTools", ToolFactory)

    result = asyncio.run(
        main.query_execute(
            QueryExecutionRequest(
                question="Who are the callers of `callee`?",
                snapshot_id=SNAPSHOT,
            )
        )
    )

    assert result.provider_id == "ollama-local"
    assert result.model_id == "qwen2.5:0.5b"
    assert result.actual_cost_usd == 0
    assert result.input_tokens is not None and result.output_tokens is not None
    assert result.latency_ms is not None and 0 < result.latency_ms < 8_000
    assert Route.GRAPH in result.plan.routes
    assert GraphOperation.CALLERS in result.plan.graph_operations
    assert result.plan.runtime_required is False
    assert result.evidence_pool.snapshot_id == SNAPSHOT


def test_local_provider_has_no_hosted_transmission_contract() -> None:
    provider = OllamaQueryAnalyzerProvider("http://127.0.0.1:11434", "qwen2.5:0.5b")

    assert provider.hosted is False
    assert provider.estimate_query_cost("question") == 0


def test_local_provider_returns_typed_usage_and_sends_only_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    payload = {
        "response": json.dumps(
            {
                "entity_mentions": [],
                "routes": ["GRAPH"],
                "graph_operations": ["CALLERS"],
                "hop_limit": 3,
                "git": None,
                "runtime_required": False,
            }
        ),
        "prompt_eval_count": 17,
        "eval_count": 8,
    }

    def fake_urlopen(request: object, *, timeout: float):
        captured["request"] = json.loads(request.data)  # type: ignore[attr-defined]
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("backend.app.local_query_analyzer.urllib.request.urlopen", fake_urlopen)
    provider = OllamaQueryAnalyzerProvider("http://127.0.0.1:11434", "qwen2.5:0.5b")
    completion = asyncio.run(provider.analyze_query("Who calls `verify_token`?"))

    assert isinstance(completion, LLMProviderCompletion)
    assert completion.usage.input_tokens == 17
    assert completion.usage.output_tokens == 8
    assert completion.usage.actual_cost_usd == 0
    request_payload = captured["request"]
    assert request_payload["prompt"].endswith("Who calls `verify_token`?")
    assert "source" in request_payload["prompt"]
    assert request_payload["options"]["num_predict"] == 48
    assert captured["timeout"] == 7.5


def test_local_provider_failure_falls_back_through_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object):
        raise OSError("local model unavailable")

    monkeypatch.setattr("backend.app.local_query_analyzer.urllib.request.urlopen", unavailable)
    analyzer = LLMQueryAnalyzer(
        OllamaQueryAnalyzerProvider("http://127.0.0.1:11434", "qwen2.5:0.5b")
    )
    result = asyncio.run(analyzer.analyze("Who calls `caller`?", snapshot_id=SNAPSHOT))

    assert not result.used_provider
    assert Route.GRAPH in result.plan.routes
    assert GraphOperation.CALLERS in result.plan.graph_operations
    assert any("proposal was rejected" in item for item in result.diagnostics)

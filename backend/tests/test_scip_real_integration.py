from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.app.claims import (
    ClaimModality,
    ClaimPredicate,
    ClaimStatus,
    ClaimType,
    ClaimValidator,
    DraftAnswer,
    DraftClaim,
)
from backend.app.config import Settings
from backend.app.evaluation import evaluate_analysis
from backend.app.fusion import EvidenceType
from backend.app.graph_tools import DeterministicGraphTools, GraphToolRequest, PathToolRequest
from backend.app.ingestion import analyze_repository
from backend.app.models import DiagnosticCode, ReplayOutcome, ReplayScope, SiteKind, SnapshotStatus
from backend.app.orchestrator import (
    GraphOperation,
    GraphToolCall,
    QueryToolExecution,
    Route,
    ToolName,
    _build_evidence_pool,
)
from backend.app.provenance import replay_edge, replay_snapshot
from backend.app.schema import validate_analysis
from backend.tests.test_ground_truth_fixture import FIXTURE, _create_history
from backend.tests.test_ingestion import init_repo

SCIP_PYTHON = os.environ.get("SCIP_PYTHON_BIN")
SCIP_CLI = os.environ.get("SCIP_CLI_BIN")

pytestmark = pytest.mark.skipif(
    not (SCIP_PYTHON and SCIP_CLI),
    reason="real SCIP integration requires SCIP_PYTHON_BIN and SCIP_CLI_BIN",
)


def test_controlled_fixture_meets_gold_graph_quality_gates(tmp_path: Path) -> None:
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    repo = tmp_path / "controlled"
    _create_history(repo)
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )
    analysis = analyze_repository(repo, settings)
    validate_analysis(analysis)

    imports = [
        {"file_path": path, "expression": expression}
        for path, by_kind in manifest["expected_structural_sites"].items()
        for expression, count in by_kind.get("IMPORT_REFERENCE", {}).items()
        for _ in range(count)
    ]
    exact_bindings = []
    reference_sites = []
    for item in manifest["expected_semantics"]:
        if item.get("target"):
            site = next(
                candidate
                for candidate in analysis.sites
                if candidate.file_path == item["path"]
                and candidate.span.start_byte == item["span"]["start_byte"]
                and candidate.kind == SiteKind.CALL_CALLEE
            )
            binding = {
                "file_path": item["path"],
                "start_byte": item["span"]["start_byte"],
                "expression": site.expression,
                "kind": site.kind.value,
                "target": item["target"],
            }
            if binding not in exact_bindings:
                exact_bindings.append(binding)
                reference_sites.append(
                    {
                        "file_path": binding["file_path"],
                        "start_byte": binding["start_byte"],
                        "expression": binding["expression"],
                        "kind": binding["kind"],
                        "status": "EXACT",
                    }
                )
        elif item.get("reason") == "dynamic attribute lookup":
            reference_sites.append(
                {
                    "file_path": item["path"],
                    "start_byte": item["span"]["start_byte"],
                    "expression": next(
                        candidate.expression
                        for candidate in analysis.sites
                        if candidate.file_path == item["path"]
                        and candidate.span.start_byte == item["span"]["start_byte"]
                        and candidate.kind == SiteKind.CALL_CALLEE
                    ),
                    "kind": SiteKind.CALL_CALLEE.value,
                    "status": "DYNAMIC",
                }
            )

    dispatch_gold = next(
        item for item in manifest["expected_semantics"] if item["id"] == "dispatch.child"
    )
    old_module = manifest["history"]["renamed_from"].removesuffix(".py").replace("/", ".")
    new_module = manifest["history"]["renamed_to"].removesuffix(".py").replace("/", ".")
    committed_definitions = [
        name.replace(f"{old_module}.", f"{new_module}.", 1)
        if name.startswith(f"{old_module}.")
        else name
        for name in manifest["expected_structural_definitions"]
    ]
    unicode_content = (repo / "pkg/checkout.py").read_bytes()
    unicode_token = "🚀 café λ".encode()
    unicode_start = unicode_content.index(unicode_token)
    gold = {
        "annotation_version": manifest["fixture_version"],
        "definitions": committed_definitions,
        "imports": imports,
        "exact_bindings": exact_bindings,
        "reference_sites": reference_sites,
        "dispatch_cases": [
            {
                "file_path": dispatch_gold["path"],
                "start_byte": dispatch_gold["span"]["start_byte"],
                "expression": "child.run",
                "candidates": dispatch_gold["candidates"],
            }
        ],
        "unicode_spans": [
            {
                "file_path": "pkg/checkout.py",
                "start_byte": unicode_start,
                "end_byte": unicode_start + len(unicode_token),
                "utf8_hex": unicode_token.hex(),
            }
        ],
    }
    sources = {
        entry.path: (repo / entry.path).read_bytes()
        for entry in analysis.manifest
        if entry.path == "pkg/checkout.py"
    }
    report = evaluate_analysis(analysis, gold, sources)
    assert report["error_taxonomy"] == {}, report["errors"]
    assert report["metrics"]["definitions"]["precision"] == 1.0
    assert report["metrics"]["definitions"]["recall"] == 1.0
    assert report["metrics"]["imports"]["precision"] == 1.0
    assert report["metrics"]["imports"]["recall"] == 1.0
    assert report["metrics"]["exact_bindings"]["precision"] == 1.0
    assert report["metrics"]["exact_bindings"]["recall"] == 1.0
    assert report["metrics"]["provenance_coverage"] == 1.0
    assert report["metrics"]["unicode_coordinate_errors"] == 0
    replay_results = replay_snapshot(analysis, settings)
    assert replay_results
    failed_records = [
        {
            "result": item.model_dump(),
            "record": next(
                record.model_dump()
                for record in analysis.provenance
                if record.edge_id == item.edge_id
            ),
        }
        for item in replay_results
        if item.outcome != ReplayOutcome.PASS
    ]
    assert not failed_records


def test_real_scip_python_output_binds_aliases_bases_and_external_symbols(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "pkg" / "dispatch_probe.py").write_text(
        """def make_meta():
    return type


class StableBase:
    def ping(self):
        return 1


class DispatchBase:
    def handle(self):
        return 1


class DispatchOne(DispatchBase):
    def handle(self):
        return 2


class DispatchTwo(DispatchBase):
    def handle(self):
        return 3


class DispatchClient:
    def call(self, value: DispatchBase):
        return value.handle()


class Plain:
    def value(self):
        return 1


class PlainClient:
    def call(self, value: Plain):
        return value.value()


class MissingBaseChild(StableBase, UnavailableBase):
    pass


class UnknownBaseClient:
    def call(self, value: MissingBaseChild):
        return value.ping()


class DynamicMeta(metaclass=make_meta()):
    def run(self):
        return 1


class DynamicMetaClient:
    def call(self, value: DynamicMeta):
        return value.run()
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "pkg/dispatch_probe.py"], check=True)
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )

    analysis = analyze_repository(repo, settings)
    validate_analysis(analysis)

    assert analysis.semantic_indexing == "INDEXED"
    assert analysis.analysis_environment is not None
    assert "0.6.6" in analysis.analysis_environment.scip_python_version
    assert "v0.10.0" in analysis.analysis_environment.scip_cli_version
    assert not any(
        item.code == DiagnosticCode.SCIP_POSITION_ENCODING_UNSPECIFIED
        for item in analysis.diagnostics
    )

    entities = {item.entity_id: item for item in analysis.entities}
    sites = {item.site_id: item for item in analysis.sites}
    alias_binding = next(
        item
        for item in analysis.bindings
        if sites[item.site_id].kind == SiteKind.CALL_CALLEE
        and sites[item.site_id].expression == "check"
    )
    assert alias_binding.target_entity_id is not None
    assert entities[alias_binding.target_entity_id].qualified_name == "pkg.auth.verify_token"
    repeated_bindings = [
        item
        for item in analysis.bindings
        if item.target_entity_id == alias_binding.target_entity_id
        and sites[item.site_id].file_path == "pkg/checkout.py"
    ]
    assert len(repeated_bindings) >= 3
    assert len({item.edge_id for item in repeated_bindings}) == len(repeated_bindings)
    semantic_edge_ids = {item.edge_id for item in repeated_bindings}
    assert semantic_edge_ids <= {item.edge_id for item in analysis.provenance}
    snapshot_replays = replay_snapshot(analysis, settings)
    assert snapshot_replays
    assert all(item.outcome == ReplayOutcome.PASS for item in snapshot_replays), [
        {
            "result": item.model_dump(),
            "record": next(
                record.model_dump()
                for record in analysis.provenance
                if record.edge_id == item.edge_id
            ),
        }
        for item in snapshot_replays
        if item.outcome != ReplayOutcome.PASS
    ]
    semantic_replay = next(
        item for item in snapshot_replays if item.edge_id == alias_binding.edge_id
    )
    assert semantic_replay.scope == ReplayScope.SNAPSHOT_SEMANTIC
    changed_environment = analysis.model_copy(
        update={
            "analysis_environment": analysis.analysis_environment.model_copy(
                update={"environment_id": "env_changed"}
            )
        }
    )
    assert (
        replay_edge(changed_environment, alias_binding.edge_id, settings).outcome
        == ReplayOutcome.ENVIRONMENT_MISMATCH
    )

    unicode_call = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/checkout.py"
        and item.kind == SiteKind.CALL_CALLEE
        and item.expression == "check"
        and item.span.start_byte
        > (tmp_path / "repo/pkg/checkout.py").read_bytes().index("🚀".encode())
    )
    unicode_binding = next(
        item for item in analysis.bindings if item.site_id == unicode_call.site_id
    )
    assert unicode_binding.target_entity_id is not None
    assert entities[unicode_binding.target_entity_id].qualified_name == "pkg.auth.verify_token"

    import_bindings = [
        item
        for item in analysis.bindings
        if sites[item.site_id].file_path == "pkg/imports.py"
        and sites[item.site_id].expression in {"normalize", "common_alias.normalize"}
    ]
    assert len(import_bindings) == 2
    assert all(
        item.target_entity_id is not None
        and entities[item.target_entity_id].qualified_name == "pkg.common.normalize"
        for item in import_bindings
    )
    assert entities[alias_binding.target_entity_id].qualified_name != "pkg.other.normalize"

    base_binding = next(
        item
        for item in analysis.bindings
        if sites[item.site_id].kind == SiteKind.BASE_CLASS_REFERENCE
        and sites[item.site_id].expression == "Base"
    )
    assert base_binding.target_entity_id is not None
    assert entities[base_binding.target_entity_id].qualified_name == "pkg.checkout.Base"

    outer_dynamic_call = next(
        item
        for item in analysis.sites
        if item.kind == SiteKind.CALL_CALLEE and item.syntax_node_type == "call"
    )
    assert all(item.site_id != outer_dynamic_call.site_id for item in analysis.bindings)
    stdlib = [item for item in analysis.external_symbols if "python-stdlib" in item.semantic_symbol]
    assert stdlib
    assert any(
        item.external_kind == "BUILTIN" and item.module_name == "builtins" for item in stdlib
    )
    assert any(item.external_kind == "STDLIB" and item.module_name == "typing" for item in stdlib)
    assert any(
        item.external_kind == "STDLIB" and item.module_name == "importlib" for item in stdlib
    )

    child_run_site = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/base_dispatch.py"
        and item.kind == SiteKind.CALL_CALLEE
        and item.expression == "child.run"
    )
    child_dispatch = next(
        item for item in analysis.dispatch if item.site_id == child_run_site.site_id
    )
    assert child_dispatch.state == "SINGLE_CANDIDATE"
    assert child_dispatch.mro_qualified_names == [
        "pkg.base_dispatch.Child",
        "pkg.base_dispatch.Left",
        "pkg.base_dispatch.Right",
        "pkg.base_dispatch.Grandparent",
        "object",
    ]
    assert [entities[item].qualified_name for item in child_dispatch.candidate_entity_ids] == [
        "pkg.base_dispatch.Left.run"
    ]
    dispatch_base_site = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/dispatch_probe.py" and item.expression == "value.handle"
    )
    dispatch_base = next(
        item for item in analysis.dispatch if item.site_id == dispatch_base_site.site_id
    )
    assert dispatch_base.state == "MULTI_CANDIDATE"
    assert {entities[item].qualified_name for item in dispatch_base.candidate_entity_ids} == {
        "pkg.dispatch_probe.DispatchBase.handle",
        "pkg.dispatch_probe.DispatchOne.handle",
        "pkg.dispatch_probe.DispatchTwo.handle",
    }
    plain_site = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/dispatch_probe.py" and item.expression == "value.value"
    )
    plain_dispatch = next(item for item in analysis.dispatch if item.site_id == plain_site.site_id)
    assert plain_dispatch.state == "SINGLE_CANDIDATE"
    assert [entities[item].qualified_name for item in plain_dispatch.candidate_entity_ids] == [
        "pkg.dispatch_probe.Plain.value"
    ]
    assert {
        (entities[edge.source_id].qualified_name, entities[edge.target_id].qualified_name)
        for edge in analysis.derived_edges
        if edge.relation == "OVERRIDES"
    } >= {
        ("pkg.base_dispatch.Left.run", "pkg.base_dispatch.Grandparent.run"),
        ("pkg.base_dispatch.Right.run", "pkg.base_dispatch.Grandparent.run"),
    }
    override_edge = next(
        edge
        for edge in analysis.derived_edges
        if edge.relation == "OVERRIDES"
        and entities[edge.source_id].qualified_name == "pkg.base_dispatch.Left.run"
    )
    assert replay_edge(analysis, override_edge.edge_id, settings).outcome == ReplayOutcome.PASS
    inheritance_premise = next(
        premise
        for premise in override_edge.premise_edge_ids
        if any(
            item.edge_id == premise and item.relation == "INHERITS"
            for item in analysis.derived_edges
        )
    )
    missing_premise_analysis = analysis.model_copy(
        update={
            "derived_edges": [
                edge for edge in analysis.derived_edges if edge.edge_id != inheritance_premise
            ]
        }
    )
    assert (
        replay_edge(missing_premise_analysis, override_edge.edge_id, settings).outcome
        == ReplayOutcome.PREMISE_MISSING
    )
    unresolved_site = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/dispatch_probe.py" and item.expression == "value.ping"
    )
    metaclass_site = next(
        item
        for item in analysis.sites
        if item.file_path == "pkg/dispatch_probe.py" and item.expression == "value.run"
    )
    unresolved_dispatch = next(
        item for item in analysis.dispatch if item.site_id == unresolved_site.site_id
    )
    metaclass_dispatch = next(
        item for item in analysis.dispatch if item.site_id == metaclass_site.site_id
    )
    assert unresolved_dispatch.state == "UNKNOWN"
    assert metaclass_dispatch.state == "UNKNOWN"
    assert unresolved_dispatch.candidate_entity_ids == []
    assert metaclass_dispatch.candidate_entity_ids == []


def test_real_scip_withholds_exact_binding_for_compound_callable_expressions(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "compound-callees")
    source = """def short():
    return 1


def substantially_longer_name():
    return 2


def factory():
    return short


class Service:
    def method(self):
        return 3


class Parent:
    def __init__(self):
        pass


class Child(Parent):
    def __init__(self):
        super().__init__()


def conditional(flag):
    return (short if flag else substantially_longer_name)()


def parenthesized_conditional(flag):
    return ((short if flag else substantially_longer_name))()


def nested_conditional(flag):
    return ((short if flag else substantially_longer_name) if flag else factory)()


def result_chain():
    return factory()()


def attribute_result_chain():
    return factory().method()


def exact_simple():
    return short()


def exact_attribute(service: Service):
    return service.method()


"""
    (repo / "pkg" / "compound.py").write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/compound.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "compound callees"], check=True)
    settings = Settings(
        allowed_repository_roots=[tmp_path],
        snapshot_cache_dir=tmp_path / "cache",
        scip_python_bin=SCIP_PYTHON,
        scip_cli_bin=SCIP_CLI,
    )

    analysis = analyze_repository(repo, settings)
    validate_analysis(analysis)

    entities = {entity.entity_id: entity.name for entity in analysis.entities}
    calls_by_context: dict[str, list[tuple[str, str | None]]] = {}
    for site in analysis.sites:
        if site.file_path != "pkg/compound.py" or site.kind != SiteKind.CALL_CALLEE:
            continue
        binding = next((item for item in analysis.bindings if item.site_id == site.site_id), None)
        calls_by_context.setdefault(entities[site.context_id], []).append(
            (site.expression, entities.get(binding.target_entity_id) if binding else None)
        )

    def targets(function: str) -> list[tuple[str, str | None]]:
        return calls_by_context[function]

    assert all(target is None for _, target in targets("conditional"))
    assert all(target is None for _, target in targets("parenthesized_conditional"))
    assert all(target is None for _, target in targets("nested_conditional"))

    result_chain = targets("result_chain")
    assert ("factory", "factory") in result_chain
    assert ("factory()", None) in result_chain
    assert ("factory().method", None) in targets("attribute_result_chain")
    assert ("factory", "factory") in targets("attribute_result_chain")
    assert targets("exact_simple") == [("short", "short")]
    assert targets("exact_attribute") == [("service.method", "method")]
    super_site = next(
        site
        for site in analysis.sites
        if site.kind == SiteKind.CALL_CALLEE and site.expression == "super().__init__"
    )
    super_binding = next(item for item in analysis.bindings if item.site_id == super_site.site_id)
    assert entities[super_binding.target_entity_id] == "__init__"

    ready_analysis = analysis.model_copy(update={"status": SnapshotStatus.READY})
    tools = DeterministicGraphTools(
        ready_analysis,
        source_loader=lambda path: (repo / path).read_bytes(),
        replay=lambda edge_id: replay_edge(ready_analysis, edge_id, settings),
    )
    entity_ids = {entity.name: entity.entity_id for entity in analysis.entities}
    strict_path = tools.find_paths(
        PathToolRequest(
            snapshot_id=analysis.snapshot_id,
            source_id=entity_ids["conditional"],
            target_id=entity_ids["short"],
            mode="STRICT_STATIC_BINDING",
        )
    )
    assert not strict_path.results

    request = GraphToolRequest(snapshot_id=analysis.snapshot_id)
    callers = tools.find_callers(request, entity_ids["short"])
    execution = QueryToolExecution(
        route=Route.GRAPH,
        operation=GraphOperation.CALLERS,
        request=GraphToolCall(
            tool=ToolName.FIND_CALLERS,
            snapshot_id=analysis.snapshot_id,
            target_id=entity_ids["short"],
        ),
        result=callers,
    )
    evidence_pool, _diagnostics = _build_evidence_pool(analysis.snapshot_id, (execution,))
    exact_simple_site = next(
        site
        for site in analysis.sites
        if site.kind == SiteKind.CALL_CALLEE and site.context_id == entity_ids["exact_simple"]
    )
    exact_simple_edge = next(
        binding.edge_id
        for binding in analysis.bindings
        if binding.site_id == exact_simple_site.site_id
    )
    unrelated_binding = next(
        item.item
        for item in evidence_pool.items
        if item.item.type == EvidenceType.STATIC_BINDING
        and item.item.source_record_id == exact_simple_edge
    )
    rejected = ClaimValidator(tools).validate(
        DraftAnswer(
            snapshot_id=analysis.snapshot_id,
            claims=(
                DraftClaim(
                    claim_id="false-conditional-call",
                    claim_type=ClaimType.DIRECT_CALL,
                    text="conditional calls short",
                    subject_id=entity_ids["conditional"],
                    predicate=ClaimPredicate.DIRECT_CALL,
                    object_id=entity_ids["short"],
                    modality=ClaimModality.EXACT,
                    evidence_ids=(unrelated_binding.evidence_id,),
                ),
            ),
        ),
        evidence_pool=evidence_pool,
        executions=(execution,),
    )
    assert rejected.claims[0].status == ClaimStatus.REJECTED

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.config import Settings
from backend.app.ingestion import (
    RepositoryConfigurationError,
    RepositoryError,
    analyze_repository,
    inspect_repository,
)
from backend.app.main import RepositoryRequest, SnapshotRequest
from backend.app.models import DerivedEdge, DiagnosticCode, ExternalSymbol, RepositoryInfo, SiteKind
from backend.app.retrieval import get_source_span, resolve_symbol
from backend.app.schema import SchemaValidationError, validate_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "python_ground_truth_repo"


def init_repo(root: Path, *, commit: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for source in (FIXTURE / "pkg").rglob("*"):
        if source.is_file():
            target = root / source.relative_to(FIXTURE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "CodeGraph Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "codegraph@example.test"], check=True
    )
    if commit:
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root


def settings(root: Path) -> Settings:
    return Settings(
        allowed_repository_roots=[root],
        snapshot_cache_dir=root / ".cache" / "snapshots",
        scip_python_bin="",
        scip_cli_bin="",
    )


def test_extracts_definitions_imports_inheritance_and_calls_with_provenance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    result = analyze_repository(repo, settings(tmp_path))

    definitions = {
        entity.qualified_name: entity
        for entity in result.entities
        if entity.kind in {"Class", "Function", "Method"}
    }
    assert {
        "pkg.auth.verify_token",
        "pkg.auth.dynamic_lookup",
        "pkg.checkout.Base",
        "pkg.checkout.Base.validate",
        "pkg.checkout.StrictCheckout",
        "pkg.checkout.StrictCheckout.validate",
        "pkg.checkout.make_checkout",
    } <= definitions.keys()
    assert any(site.kind == SiteKind.IMPORT_REFERENCE for site in result.sites)
    assert any(
        site.kind == SiteKind.BASE_CLASS_REFERENCE and site.expression == "Base"
        for site in result.sites
    )

    checkout = (repo / "pkg" / "checkout.py").read_bytes()
    call = next(
        site
        for site in result.sites
        if site.kind == SiteKind.CALL_CALLEE and site.expression == "check"
    )
    assert checkout[call.span.start_byte : call.span.end_byte] == b"check"
    assert call.span.start_byte > checkout.index("🚀".encode())
    assert result.provenance
    assert len({record.provenance_id for record in result.provenance}) == len(result.provenance)
    assert result.semantic_indexing == "UNAVAILABLE"
    assert any(item.code == DiagnosticCode.SCIP_UNAVAILABLE for item in result.diagnostics)


def test_structural_import_edges_resolve_local_module_with_provenance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    result = analyze_repository(repo, settings(tmp_path))
    modules = {item.entity_id: item for item in result.entities if item.kind == "Module"}
    imports = [
        item
        for item in result.imports
        if item.imported_module == "pkg.common"
        and modules[item.source_module_id].qualified_name == "pkg.imports"
    ]

    assert imports
    relation = imports[0]
    assert modules[relation.source_module_id].qualified_name == "pkg.imports"
    assert modules[relation.target_module_id or ""].qualified_name == "pkg.common"
    assert relation.target_external_symbol_id is None
    assert relation.resolver == "repository-module-index"
    evidence = next(item for item in result.provenance if item.edge_id == relation.edge_id)
    assert evidence.rule_id == "imports.repository-module-index.v1"
    assert evidence.file_path == "pkg/imports.py"
    assert evidence.span.start_byte == relation.span.start_byte
    validate_analysis(result)


def test_nested_function_is_not_mislabeled_as_method(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    source = repo / "pkg" / "checkout.py"
    source.write_text(
        """class Service:
    def run(self):
        def helper():
            return 1
        return helper()
""",
        encoding="utf-8",
    )
    result = analyze_repository(repo, settings(tmp_path))
    helper = next(entity for entity in result.entities if entity.name == "helper")
    assert helper.kind == "Function"
    assert helper.qualified_name.endswith("Service.run.helper")


def test_snapshot_identity_tracks_worktree_content_and_cached_blob_is_immutable(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)
    first = analyze_repository(repo, config)
    original_id = first.snapshot_id
    original_content = (repo / "pkg" / "auth.py").read_bytes()
    dirty_content = original_content + b"\n"
    (repo / "pkg" / "auth.py").write_bytes(dirty_content)
    dirty = analyze_repository(repo, config)
    (repo / "pkg" / "auth.py").write_text(
        "def verify_token(token):\n    return False\n", encoding="utf-8"
    )
    second = analyze_repository(repo, config)

    assert second.snapshot_id != original_id
    auth_hash = next(entry.blob_hash for entry in first.manifest if entry.path == "pkg/auth.py")
    blob = config.snapshot_cache_dir / original_id / "blobs" / auth_hash
    assert blob.read_bytes() == original_content
    dirty_entry = next(entry for entry in dirty.manifest if entry.path == "pkg/auth.py")
    dirty_blob = config.snapshot_cache_dir / dirty.snapshot_id / "blobs" / dirty_entry.blob_hash
    assert dirty_blob.read_bytes() == dirty_content


def test_clean_snapshot_identity_is_repeatable_for_same_tree_and_configuration(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)

    first = analyze_repository(repo, config)
    second = analyze_repository(repo, config)

    assert first.repository.repository_id == second.repository.repository_id
    assert first.snapshot_id == second.snapshot_id
    assert not first.repository.is_dirty


def test_one_byte_change_and_distinct_dirty_states_at_same_head_have_distinct_ids(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)
    source = repo / "pkg" / "auth.py"
    original = source.read_bytes()
    clean = analyze_repository(repo, config)

    source.write_bytes(original + b"\n")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/auth.py"], check=True)
    first_dirty = analyze_repository(repo, config)
    source.write_bytes(original + b" ")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/auth.py"], check=True)
    second_dirty = analyze_repository(repo, config)

    assert len(source.read_bytes()) == len(original) + 1
    assert clean.repository.head_commit == first_dirty.repository.head_commit
    assert first_dirty.repository.head_commit == second_dirty.repository.head_commit
    assert first_dirty.repository.is_dirty and second_dirty.repository.is_dirty
    assert len({clean.snapshot_id, first_dirty.snapshot_id, second_dirty.snapshot_id}) == 3


def test_repository_id_is_persisted_and_stable_across_new_commits(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    config = settings(tmp_path)
    _, before = inspect_repository(repo, config)
    (repo / "new.txt").write_text("new commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "new.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second"], check=True)

    _, after = inspect_repository(repo, config)

    assert after.repository_id == before.repository_id
    registry = config.snapshot_cache_dir / "repositories"
    records = list(registry.glob("*.json"))
    assert len(records) == 1
    assert before.repository_id in records[0].read_text(encoding="utf-8")


def test_remote_repository_id_uses_normalized_remote_identity(tmp_path: Path) -> None:
    first_repo = init_repo(tmp_path / "first")
    second_repo = init_repo(tmp_path / "second")
    remote = "https://git@github.com/example/project.git"
    for repo in (first_repo, second_repo):
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)

    _, first = inspect_repository(first_repo, Settings(allowed_repository_roots=[tmp_path]))
    _, second = inspect_repository(second_repo, Settings(allowed_repository_roots=[tmp_path]))

    assert first.remote == "https://github.com/example/project"
    assert first.repository_id == second.repository_id


def test_file_discovery_skips_venvs_reports_notebooks_and_keeps_stubs_and_vendor(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    additions = {
        ".venv/lib/python3.11/site-packages/hidden.py": "def hidden(): pass\n",
        "notes.ipynb": '{"cells": [], "nbformat": 4}\n',
        "pkg/contracts.pyi": "def public(value: str) -> bool: ...\n",
        "vendor/dependency.py": "def vendored(): pass\n",
        "pkg/too_large.py": "x = '" + "a" * 1800 + "'\n",
    }
    for relative, contents in additions.items():
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    analysis = analyze_repository(
        repo,
        Settings(
            allowed_repository_roots=[tmp_path],
            snapshot_cache_dir=tmp_path / "cache",
            scip_python_bin="",
            scip_cli_bin="",
            max_source_file_bytes=1800,
        ),
    )

    entries = {entry.path: entry for entry in analysis.manifest}
    assert not any(".venv/" in path for path in entries)
    assert "notes.ipynb" not in entries
    assert entries["pkg/contracts.pyi"].analyzed
    assert entries["vendor/dependency.py"].classification == "VENDORED"
    assert not entries["pkg/too_large.py"].analyzed
    assert any(
        item.code == DiagnosticCode.NOTEBOOK_UNSUPPORTED and item.file_path == "notes.ipynb"
        for item in analysis.diagnostics
    )
    assert any(
        item.code == DiagnosticCode.VENDORED_CODE and item.file_path == "vendor/dependency.py"
        for item in analysis.diagnostics
    )
    assert any(
        item.code == DiagnosticCode.FILE_TOO_LARGE and item.file_path == "pkg/too_large.py"
        for item in analysis.diagnostics
    )


def test_repository_info_detects_shallow_and_detached_checkouts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow)], check=True)
    _, shallow_info = inspect_repository(shallow, Settings(allowed_repository_roots=[tmp_path]))
    assert shallow_info.is_shallow
    assert not shallow_info.is_detached

    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "HEAD"], check=True)
    _, detached_info = inspect_repository(repo, Settings(allowed_repository_roots=[tmp_path]))
    assert not detached_info.is_shallow
    assert detached_info.is_detached


def test_repository_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    repo = init_repo(tmp_path / "outside" / "repo")
    allowed.mkdir()
    with pytest.raises(RepositoryError, match="ALLOWED_REPOSITORY_ROOTS"):
        inspect_repository(repo, Settings(allowed_repository_roots=[allowed]))


def test_repository_inspection_fails_closed_when_allowlist_is_unset_or_empty(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    unset = Settings(_env_file=None)
    empty = Settings(_env_file=None, allowed_repository_roots="")

    assert unset.allowed_repository_roots == []
    assert empty.allowed_repository_roots == []
    for config in (unset, empty):
        with pytest.raises(RepositoryConfigurationError, match="ALLOWED_REPOSITORY_ROOTS"):
            inspect_repository(repo, config)


def test_git_discovery_preserves_leading_and_trailing_filename_spaces(tmp_path: Path) -> None:
    from backend.app.ingestion import _discover, _git

    repo = init_repo(tmp_path / "repo")
    expected = "pkg/ leading.py"
    trailing_space = "pkg/trailing file "
    (repo / expected).write_text("def preserved():\n    return 1\n", encoding="utf-8")
    (repo / trailing_space).write_text("tracked payload\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", expected, trailing_space], check=True)

    files, _diagnostics = _discover(repo, include_untracked=False)
    exact_git_paths = _git(repo, "ls-files", "-z").split("\0")

    assert expected in {path.relative_to(repo).as_posix() for path in files}
    assert trailing_space in exact_git_paths


def test_path_api_routes_return_clear_configuration_errors_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(main, "settings", Settings(_env_file=None))

    with pytest.raises(HTTPException) as repository_error:
        main.repository_inspect(RepositoryRequest(path=str(repo)))
    with pytest.raises(HTTPException) as registration_error:
        main.repository_register(RepositoryRequest(path=str(repo)))
    with pytest.raises(HTTPException) as snapshot_error:
        main.snapshot_inspect(SnapshotRequest(path=str(repo)))
    with pytest.raises(HTTPException) as ingest_error:
        main.snapshot_ingest(SnapshotRequest(path=str(repo)))

    for error in (
        repository_error.value,
        registration_error.value,
        snapshot_error.value,
        ingest_error.value,
    ):
        assert error.status_code == 503
        assert "ALLOWED_REPOSITORY_ROOTS" in error.detail


def test_inspect_request_cannot_request_publication() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="publish"):
        SnapshotRequest(path="/tmp/repo", publish=True)


def test_snapshot_inspect_never_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    monkeypatch.setattr(main, "settings", settings(tmp_path))
    monkeypatch.setattr(main, "analyze_repository", lambda *_args, **_kwargs: analysis)

    class MustNotPublish:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("inspect attempted to create a publication store")

    monkeypatch.setattr(main, "GraphStore", MustNotPublish)
    assert main.snapshot_inspect(SnapshotRequest(path=str(repo))) is analysis


def test_repository_registration_persists_repository_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(main, "settings", settings(tmp_path))
    registered: list[str] = []

    class Store:
        def __init__(self, _settings: Settings) -> None:
            pass

        def register_repository(self, info: RepositoryInfo) -> str:
            registered.append(info.repository_id)
            return registered[-1]

    monkeypatch.setattr(main, "GraphStore", Store)
    result = main.repository_register(RepositoryRequest(path=str(repo)))

    assert result["repository_id"] == registered[0]
    assert result["head_commit"]
    assert result["is_dirty"] is False


def test_repository_inside_configured_root_is_accepted(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    repo = init_repo(allowed / "nested" / "repo")

    config = Settings(_env_file=None, allowed_repository_roots=str(allowed))
    root, info = inspect_repository(repo, config)

    assert root == repo.resolve()
    assert info.root == str(repo.resolve())


def test_relative_allowlist_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute paths"):
        Settings(_env_file=None, allowed_repository_roots=".")


def test_repository_symlink_cannot_escape_configured_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside_repo = init_repo(tmp_path / "outside" / "repo")
    escape = allowed / "linked-repository"
    escape.symlink_to(outside_repo, target_is_directory=True)

    with pytest.raises(RepositoryError, match="outside ALLOWED_REPOSITORY_ROOTS"):
        inspect_repository(escape, Settings(allowed_repository_roots=[allowed]))


def test_external_symlink_is_omitted_with_diagnostic(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.py"
    outside.write_text("def secret():\n    return None\n", encoding="utf-8")
    link = repo / "pkg" / "escape.py"
    link.symlink_to(outside)
    subprocess.run(["git", "-C", str(repo), "add", "pkg/escape.py"], check=True)

    result = analyze_repository(repo, settings(tmp_path))
    assert not any(entry.path == "pkg/escape.py" for entry in result.manifest)
    assert any(
        item.code == DiagnosticCode.PATH_REJECTED and item.file_path == "pkg/escape.py"
        for item in result.diagnostics
    )


def test_partial_parse_reports_error_and_drops_sites_inside_error_region(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    source = repo / "pkg" / "auth.py"
    source.write_text(
        "def ok():\n    return valid()\n\ndef broken(:\n    return fabricated()\n", encoding="utf-8"
    )
    result = analyze_repository(repo, settings(tmp_path))
    assert any(item.code == DiagnosticCode.PARTIAL_PARSE for item in result.diagnostics)
    assert not any(site.expression == "fabricated" for site in result.sites)
    assert any(site.expression == "valid" for site in result.sites)


def test_untracked_files_are_opt_in(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "untracked.py").write_text("def new_symbol():\n    pass\n", encoding="utf-8")
    config = settings(tmp_path)
    without = analyze_repository(repo, config)
    with_untracked = analyze_repository(repo, config, include_untracked=True)
    assert "untracked.py" not in {entry.path for entry in without.manifest}
    assert "untracked.py" in {entry.path for entry in with_untracked.manifest}


def test_schema_validator_accepts_complete_parser_provenance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    validate_analysis(analyze_repository(repo, settings(tmp_path)))


def test_schema_validator_rejects_missing_provenance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    corrupted = analysis.model_copy(update={"provenance": analysis.provenance[1:]})
    with pytest.raises(SchemaValidationError, match="provenance records"):
        validate_analysis(corrupted)


def test_schema_validator_rejects_cross_snapshot_provenance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    record = analysis.provenance[0].model_copy(update={"snapshot_id": "snap_other"})
    corrupted = analysis.model_copy(update={"provenance": [record, *analysis.provenance[1:]]})
    with pytest.raises(SchemaValidationError, match="crosses snapshot scope"):
        validate_analysis(corrupted)


def test_schema_validator_rejects_document_local_external_identity(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    assert analysis.analysis_environment is not None
    invalid = ExternalSymbol(
        external_symbol_id="ext_local_collision",
        semantic_symbol="local 5",
        environment_id=analysis.analysis_environment.environment_id,
    )
    corrupted = analysis.model_copy(
        update={"external_symbols": [*analysis.external_symbols, invalid]}
    )
    with pytest.raises(SchemaValidationError, match="document-local"):
        validate_analysis(corrupted)


def test_schema_validator_rejects_invalid_and_cross_snapshot_relation_endpoints(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    class_entity = next(item for item in analysis.entities if item.kind == "Class")
    file_entity = next(item for item in analysis.entities if item.kind == "File")
    invalid_endpoint = DerivedEdge(
        edge_id="edge_invalid_endpoint",
        relation="INHERITS",
        source_id=file_entity.entity_id,
        target_id=class_entity.entity_id,
        rule_id="test.invalid-endpoint",
    )
    with pytest.raises(SchemaValidationError, match="non-class source"):
        validate_analysis(analysis.model_copy(update={"derived_edges": [invalid_endpoint]}))

    cross_snapshot = invalid_endpoint.model_copy(
        update={
            "edge_id": "edge_cross_snapshot",
            "source_id": class_entity.entity_id,
            "target_id": "sym_from_other_snapshot",
        }
    )
    with pytest.raises(SchemaValidationError, match="missing endpoint"):
        validate_analysis(analysis.model_copy(update={"derived_edges": [cross_snapshot]}))


def test_exact_symbol_resolution_reports_ambiguous_simple_names(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    result = resolve_symbol(analysis, "validate")
    assert result.status == "AMBIGUOUS"
    assert result.matched_by == "NAME"
    assert {entity.qualified_name for entity in result.results} == {
        "pkg.checkout.Base.validate",
        "pkg.checkout.StrictCheckout.validate",
        "pkg.checkout.FastCheckout.validate",
    }

    exact = resolve_symbol(analysis, "pkg.checkout.StrictCheckout.validate")
    assert exact.status == "UNIQUE"
    assert exact.matched_by == "QUALIFIED_NAME"


def test_identifier_retrieval_prefers_exact_name_then_matches_normalized_tokens(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))

    exact = resolve_symbol(analysis, "verify_token")
    assert exact.status == "UNIQUE"
    assert exact.matched_by == "NAME"
    assert exact.results[0].qualified_name == "pkg.auth.verify_token"

    token = resolve_symbol(analysis, "token")
    assert token.status == "UNIQUE"
    assert token.matched_by == "FULL_TEXT"
    assert token.results[0].qualified_name == "pkg.auth.verify_token"


def test_payment_processor_normalizes_camel_case_identifier() -> None:
    from backend.app.identifiers import normalize_identifier

    assert normalize_identifier("PaymentProcessor") == "payment processor"


def test_source_span_replays_exact_unicode_prefixed_bytes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    analysis = analyze_repository(repo, settings(tmp_path))
    site = next(site for site in analysis.sites if site.expression == "check")
    source = (repo / site.file_path).read_bytes()
    result = get_source_span(
        analysis, site.file_path, site.span.start_byte, site.span.end_byte, source
    )
    assert result.text == "check"
    assert result.start_byte == site.span.start_byte
    assert result.start_line == site.span.start_line

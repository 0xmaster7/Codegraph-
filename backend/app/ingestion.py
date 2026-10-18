from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tree_sitter_language_pack import get_parser

from .chunking import build_document_chunks, build_source_chunks
from .config import Settings
from .dependencies import extract_dependency_metadata
from .dispatch import derive_dispatch
from .environment import capture_environment
from .history import extract_git_history
from .ids import stable_id
from .imports import build_import_relationships
from .materialization import (
    capture_runtime_materialization,
    load_runtime_materialization,
)
from .models import (
    Diagnostic,
    DiagnosticCode,
    ExternalSymbol,
    ProvenanceRecord,
    RepositoryInfo,
    RetrievalChunk,
    SemanticBinding,
    SiteKind,
    SnapshotAnalysis,
    SnapshotManifestEntry,
    SnapshotStatus,
    SourceEntity,
    SourceSite,
    SourceSpan,
)
from .scip import join_bindings, run_scip_index, scip_toolchain
from .securefs import RepositoryPathEscape, read_repository_file

SOURCE_SUFFIXES = {".py", ".pyi"}
DOCUMENT_SUFFIXES = {".md", ".markdown"}
CONFIG_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "pyrightconfig.json",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    ".mailmap",
    "setup.py",
}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "node_modules",
    ".tox",
    ".nox",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


class RepositoryError(ValueError):
    pass


class RepositoryConfigurationError(RepositoryError):
    pass


class AnalysisCancelled(RepositoryError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if check and result.returncode:
        raise RepositoryError(result.stderr.strip() or f"git {' '.join(args)} failed")
    # Git's path-producing commands may return legal leading/trailing spaces.
    # Remove only the command's final record terminator, never path data.
    return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _persist_repository_id(identity: str, cache_dir: Path) -> str:
    repository_id = stable_id(identity, prefix="repo_")
    registry_dir = cache_dir.expanduser().resolve() / "repositories"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = registry_dir / f"{_sha(identity.encode('utf-8'))}.json"
    if registry_file.exists():
        try:
            stored = json.loads(registry_file.read_text(encoding="utf-8"))
            if stored.get("identity") == identity and stored.get("repository_id"):
                return str(stored["repository_id"])
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    temporary = registry_file.with_name(f"{registry_file.name}.{os.urandom(8).hex()}.tmp")
    temporary.write_text(
        json.dumps({"identity": identity, "repository_id": repository_id}, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, registry_file)
    return repository_id


def _classification(path: str, content: bytes) -> str:
    parts = {part.lower() for part in Path(path).parts}
    if parts.intersection({"vendor", "vendors", "third_party", "third-party", "extern"}):
        return "VENDORED"
    name = Path(path).name.lower()
    if (
        name.endswith("_generated.py")
        or name.startswith("generated_")
        or b"@generated" in content[:2048].lower()
    ):
        return "GENERATED"
    return "PROJECT"


def inspect_repository(
    path: str | Path,
    settings: Settings,
    *,
    repository_id_override: str | None = None,
    skip_worktree_status: bool = False,
) -> tuple[Path, RepositoryInfo]:
    roots = [root.expanduser().resolve() for root in settings.allowed_repository_roots]
    if not roots:
        raise RepositoryConfigurationError(
            "Repository inspection is disabled: configure ALLOWED_REPOSITORY_ROOTS "
            "with one or more permitted directory paths."
        )
    requested = Path(path).expanduser().resolve(strict=True)
    if not requested.is_dir():
        raise RepositoryError("Repository path must be a directory")
    if not any(requested == root or root in requested.parents for root in roots):
        raise RepositoryError("Repository is outside ALLOWED_REPOSITORY_ROOTS")
    try:
        root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if not any(root == allowed or allowed in root.parents for allowed in roots):
            raise RepositoryError("Git repository root is outside ALLOWED_REPOSITORY_ROOTS")
        common_dir = Path(_git(root, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()
        head = _git(root, "rev-parse", "HEAD", check=False) or None
        roots_output = _git(root, "rev-list", "--max-parents=0", "HEAD", check=False)
        root_commit = sorted(roots_output.splitlines())[0] if roots_output else "unborn"
        remote_names = _git(root, "remote", check=False).splitlines()
        selected_remote = (
            "origin" if "origin" in remote_names else next(iter(sorted(remote_names)), None)
        )
        remote = (
            _git(root, "remote", "get-url", selected_remote, check=False) if selected_remote else ""
        ) or None
        normalized_remote = remote.rstrip("/").removesuffix(".git") if remote else None
        if normalized_remote and "://" in normalized_remote:
            parsed = urlsplit(normalized_remote)
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            normalized_remote = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        identity = normalized_remote or f"{common_dir}:{root_commit}"
        if repository_id_override is not None and not re.fullmatch(
            r"repo_[0-9a-f]{64}", repository_id_override
        ):
            raise RepositoryError("Invalid internal repository identity override")
        repo_id = repository_id_override or _persist_repository_id(
            identity, settings.snapshot_cache_dir
        )
        dirty = (
            False
            if skip_worktree_status
            else bool(_git(root, "status", "--porcelain", "--untracked-files=all"))
        )
        shallow = _git(root, "rev-parse", "--is-shallow-repository", check=False) == "true"
        detached = bool(head) and not bool(
            _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        )
        return root, RepositoryInfo(
            repository_id=repo_id,
            root=str(root),
            remote=normalized_remote,
            head_commit=head,
            is_dirty=dirty,
            is_shallow=shallow,
            is_detached=detached,
        )
    except (OSError, RepositoryError) as exc:
        if isinstance(exc, RepositoryError):
            raise
        raise RepositoryError("Path is not a readable Git repository") from exc


def _discover(root: Path, include_untracked: bool) -> tuple[list[Path], list[Diagnostic]]:
    tracked = set(_git(root, "ls-files", "-z").split("\0"))
    if include_untracked:
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        tracked.update(untracked.split("\0"))
    paths: list[Path] = []
    diagnostics: list[Diagnostic] = []
    for rel in sorted(item for item in tracked if item):
        relative = Path(rel)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.suffix == ".ipynb":
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.NOTEBOOK_UNSUPPORTED,
                    message="Notebook indexing is unsupported in V1.",
                    file_path=rel,
                )
            )
            continue
        is_requirement_file = relative.suffix == ".txt" and (
            relative.name.startswith(("requirements", "constraints"))
            or relative.parts[0] in {"requirements", "constraints"}
        )
        if (
            relative.suffix not in SOURCE_SUFFIXES
            and relative.suffix.lower() not in DOCUMENT_SUFFIXES
            and relative.name not in CONFIG_NAMES
            and not is_requirement_file
        ):
            continue
        try:
            opened = read_repository_file(root, rel)
        except FileNotFoundError:
            continue
        except RepositoryPathEscape:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.PATH_REJECTED,
                    message="Symlink resolves outside repository root.",
                    file_path=rel,
                )
            )
            continue
        if opened is not None:
            paths.append(root / relative)
    return paths, diagnostics


def _collect_manifest(
    root: Path, files: list[Path], settings: Settings
) -> tuple[list[SnapshotManifestEntry], dict[str, bytes], dict[str, bytes], list[Diagnostic]]:
    manifest: list[SnapshotManifestEntry] = []
    sources: dict[str, bytes] = {}
    materialized_files: dict[str, bytes] = {}
    diagnostics: list[Diagnostic] = []
    for file in files:
        rel = file.relative_to(root).as_posix()
        try:
            opened = read_repository_file(root, rel)
        except RepositoryPathEscape as exc:
            raise RepositoryError(f"Repository path escaped its authorized root: {rel}") from exc
        if opened is None:
            continue
        content, _metadata = opened
        classification = (
            _classification(rel, content) if file.suffix in SOURCE_SUFFIXES else "PROJECT"
        )
        if classification == "VENDORED":
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.VENDORED_CODE,
                    message="Tracked vendored source is included and classified separately.",
                    file_path=rel,
                )
            )
        if len(content) > settings.max_source_file_bytes and file.suffix in SOURCE_SUFFIXES:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.FILE_TOO_LARGE,
                    message=f"File exceeds {settings.max_source_file_bytes} byte limit.",
                    file_path=rel,
                )
            )
            manifest.append(
                SnapshotManifestEntry(
                    path=rel,
                    content_hash=_sha(content),
                    blob_hash=_sha(content),
                    size_bytes=len(content),
                    analyzed=False,
                    classification=classification,
                )
            )
            continue
        digest = _sha(content)
        manifest.append(
            SnapshotManifestEntry(
                path=rel,
                content_hash=digest,
                blob_hash=digest,
                size_bytes=len(content),
                analyzed=True,
                classification=classification,
            )
        )
        materialized_files[rel] = content
        if file.suffix in SOURCE_SUFFIXES:
            sources[rel] = content
    return manifest, sources, materialized_files, diagnostics


def _snapshot_identity(
    repository: RepositoryInfo,
    manifest: list[SnapshotManifestEntry],
    settings: Settings,
    include_untracked: bool,
) -> str:
    source_manifest_hash = stable_id(*[(m.path, m.content_hash, m.analyzed) for m in manifest])
    _, _, scip_python_version, scip_cli_version = scip_toolchain(settings)
    environment = capture_environment(
        source_manifest_hash,
        scip_python_version,
        scip_cli_version,
    )
    try:
        parser_version = importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        parser_version = "unknown"
    analysis_config_hash = stable_id(
        "analysis-config-v1",
        "tree-sitter-python",
        parser_version,
        settings.max_source_file_bytes,
        include_untracked,
        environment.environment_id,
    )
    return stable_id(
        repository.repository_id,
        repository.head_commit,
        source_manifest_hash,
        analysis_config_hash,
        prefix="snap_",
    )


def identify_snapshot(
    path: str | Path,
    settings: Settings,
    include_untracked: bool = False,
    *,
    repository_id_override: str | None = None,
    skip_worktree_status: bool = False,
) -> tuple[str, RepositoryInfo]:
    """Compute the deterministic snapshot key before scheduling an ingestion job."""
    root, repository = inspect_repository(
        path,
        settings,
        repository_id_override=repository_id_override,
        skip_worktree_status=skip_worktree_status,
    )
    files, _ = _discover(root, include_untracked)
    manifest, _, _, _ = _collect_manifest(root, files, settings)
    return _snapshot_identity(repository, manifest, settings, include_untracked), repository


def _span(node: Any) -> SourceSpan:
    return SourceSpan(
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        start_line=node.start_point.row + 1,
        start_column=node.start_point.column,
        end_line=node.end_point.row + 1,
        end_column=node.end_point.column,
    )


def _field_text(node: Any, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return source[child.start_byte : child.end_byte].decode("utf-8", "replace") if child else None


def _parse_file(
    snapshot_id: str, path: str, content: bytes, version: str
) -> tuple[list[SourceEntity], list[SourceSite], list[ProvenanceRecord], list[Diagnostic]]:
    parser = get_parser("python")
    tree = parser.parse(content)
    entities: list[SourceEntity] = []
    sites: list[SourceSite] = []
    provenance: list[ProvenanceRecord] = []
    diagnostics: list[Diagnostic] = []
    digest = _sha(content)
    module_name = path[:-4] if path.endswith(".pyi") else path.removesuffix(".py")
    module_name = module_name.replace("/", ".").replace("\\", ".")
    if module_name.endswith(".__init__"):
        module_name = module_name[:-9]
    module_id = stable_id(snapshot_id, path, "module", prefix="mod_")
    file_id = stable_id(snapshot_id, path, digest, "file", prefix="file_")
    module_span = SourceSpan(
        start_byte=0,
        end_byte=len(content),
        start_line=1,
        start_column=0,
        end_line=max(1, content.count(b"\n") + 1),
        end_column=len(content.rsplit(b"\n", 1)[-1]),
    )
    module = SourceEntity(
        entity_id=module_id,
        kind="Module",
        name=module_name.rsplit(".", 1)[-1],
        qualified_name=module_name,
        file_path=path,
        span=module_span,
        parent_id=file_id,
    )
    file_entity = SourceEntity(
        entity_id=file_id,
        kind="File",
        name=Path(path).name,
        qualified_name=path,
        file_path=path,
        span=module_span,
    )
    entities.extend([file_entity, module])

    for relation, source_id, target_id, rule in (
        ("CONTAINS", snapshot_id, file_id, "snapshot.contains-file.v1"),
        ("DECLARES_MODULE", file_id, module_id, "file.declares-module.v1"),
    ):
        edge_id = stable_id("static", relation, source_id, target_id, 0, prefix="edge_")
        provenance.append(
            ProvenanceRecord(
                provenance_id=stable_id(edge_id, prefix="prov_"),
                edge_id=edge_id,
                snapshot_id=snapshot_id,
                file_path=path,
                file_content_hash=digest,
                span=module_span,
                syntax_node_type="source_file",
                extractor_version=version,
                rule_id=rule,
            )
        )
    error_nodes: list[Any] = []

    def collect_errors(node: Any) -> None:
        if node.type == "ERROR" or node.is_missing:
            error_nodes.append(node)
        for child in node.children:
            collect_errors(child)

    collect_errors(tree.root_node)

    def overlaps_error(node: Any) -> bool:
        return any(
            error.start_byte <= node.start_byte < error.end_byte
            or node.start_byte <= error.start_byte < node.end_byte
            for error in error_nodes
        )

    def is_callback_reference(node: Any) -> bool:
        if node.type != "identifier" or node.parent is None:
            return False
        parent = node.parent
        if parent.type == "default_parameter":
            parameter_name = _field_text(parent, "name", content)
            return parent.child_by_field_name("value") == node and parameter_name in {
                "fn",
                "func",
                "callback",
                "handler",
                "target",
            }
        if parent.type == "keyword_argument":
            parameter_name = _field_text(parent, "name", content)
            argument_list = parent.parent
            call = (
                argument_list.parent
                if argument_list and argument_list.type == "argument_list"
                else None
            )
            return (
                parent.child_by_field_name("value") == node
                and call is not None
                and call.type == "call"
                and parameter_name in {"fn", "func", "callback", "handler", "target", "key"}
            )
        if parent.type != "argument_list" or parent.parent is None:
            return False
        call = parent.parent
        if call.type != "call" or call.child_by_field_name("arguments") != parent:
            return False
        callee_name = _field_text(call, "function", content) or ""
        if callee_name.rsplit(".", 1)[-1] not in {"map", "filter", "submit", "apply_async"}:
            return False
        # These APIs take a callable in their first positional argument; record a
        # candidate site without asserting that the identifier resolves to it.
        return bool(parent.named_children and parent.named_children[0] == node)

    def walk(
        node: Any, context: SourceEntity, scopes: list[str], class_context: bool = False
    ) -> None:
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition is None:
                return
            for child in node.named_children:
                if child.type != "decorator" or not child.named_children:
                    continue
                expression = child.named_children[-1]
                if expression.type == "call":
                    expression = expression.child_by_field_name("function") or expression
                add_site(expression, context, SiteKind.DECORATOR_REFERENCE)
            walk(definition, context, scopes, class_context)
            return
        if node.type in {"function_definition", "class_definition"}:
            if overlaps_error(node):
                return
            name = _field_text(node, "name", content) or "<anonymous>"
            kind = (
                "Class"
                if node.type == "class_definition"
                else ("Method" if class_context else "Function")
            )
            qname = ".".join([*scopes, name])
            span = _span(node)
            entity = SourceEntity(
                entity_id=stable_id(
                    snapshot_id, path, span.start_byte, span.end_byte, kind, prefix="sym_"
                ),
                kind=kind,
                name=name,
                qualified_name=qname,
                file_path=path,
                span=span,
                parent_id=context.entity_id,
            )
            entities.append(entity)
            definition_relation = "DEFINES_METHOD" if kind == "Method" else "DEFINES"
            edge = stable_id(
                "static",
                definition_relation,
                context.entity_id,
                entity.entity_id,
                span.start_byte,
                prefix="edge_",
            )
            provenance.append(
                ProvenanceRecord(
                    provenance_id=stable_id(edge, prefix="prov_"),
                    edge_id=edge,
                    snapshot_id=snapshot_id,
                    file_path=path,
                    file_content_hash=digest,
                    span=span,
                    syntax_node_type=node.type,
                    extractor_version=version,
                    rule_id="syntax.definition.v1",
                )
            )
            body = node.child_by_field_name("body")
            if kind == "Class":
                superclasses = node.child_by_field_name("superclasses")
                if superclasses:
                    for base in superclasses.named_children:
                        add_site(base, context, SiteKind.BASE_CLASS_REFERENCE)
            for child in node.children:
                if child == body:
                    walk(child, entity, [*scopes, name], kind == "Class")
                else:
                    walk(child, context, scopes, class_context)
            return
        if node.type == "call":
            function = node.child_by_field_name("function")
            if function:
                add_site(function, context, SiteKind.CALL_CALLEE)
        if is_callback_reference(node):
            add_site(node, context, SiteKind.CALLBACK_REFERENCE)
        if node.type in {"import_statement", "import_from_statement"}:
            add_site(node, context, SiteKind.IMPORT_REFERENCE)
        for child in node.named_children:
            walk(child, context, scopes, class_context)

    def add_site(node: Any, context: SourceEntity, kind: SiteKind) -> None:
        # A call site's evidence points to the callee span, not the entire argument list.
        span = _span(node)
        if overlaps_error(node):
            return
        expression = content[span.start_byte : span.end_byte].decode("utf-8", "replace")
        site_id = stable_id(
            snapshot_id, path, span.start_byte, span.end_byte, kind.value, prefix="site_"
        )
        sites.append(
            SourceSite(
                site_id=site_id,
                kind=kind,
                expression=expression,
                file_path=path,
                span=span,
                context_id=context.entity_id,
                syntax_node_type=node.type,
            )
        )
        edge = stable_id(
            "static", "OCCURS_IN", site_id, context.entity_id, span.start_byte, prefix="edge_"
        )
        provenance.append(
            ProvenanceRecord(
                provenance_id=stable_id(edge, prefix="prov_"),
                edge_id=edge,
                snapshot_id=snapshot_id,
                file_path=path,
                file_content_hash=digest,
                span=span,
                syntax_node_type=node.type,
                extractor_version=version,
                rule_id="syntax.site-context.v1",
            )
        )

    # Hoist the nested helper so walk can register decorator sites.
    walk(tree.root_node, module, [module_name])
    for error in error_nodes:
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.PARTIAL_PARSE,
                message="Parser reported an error region; overlapping sites are omitted.",
                file_path=path,
                span=_span(error),
            )
        )
    return entities, sites, provenance, diagnostics


def analyze_repository(
    path: str | Path,
    settings: Settings,
    include_untracked: bool = False,
    *,
    cancellation_event: threading.Event | None = None,
    on_stage: Callable[[str], None] | None = None,
    expected_snapshot_id: str | None = None,
    expected_runtime_materialization_id: str | None = None,
    repository_id_override: str | None = None,
    skip_worktree_status: bool = False,
) -> SnapshotAnalysis:
    def check_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise AnalysisCancelled("Ingestion was cancelled")

    check_cancelled()
    root, repo = inspect_repository(
        path,
        settings,
        repository_id_override=repository_id_override,
        skip_worktree_status=skip_worktree_status,
    )
    files, diagnostics = _discover(root, include_untracked)
    manifest, sources, materialized_files, manifest_diagnostics = _collect_manifest(
        root, files, settings
    )
    diagnostics.extend(manifest_diagnostics)
    source_manifest_hash = stable_id(*[(m.path, m.content_hash, m.analyzed) for m in manifest])
    _, _, scip_python_version, scip_cli_version = scip_toolchain(settings)
    environment = capture_environment(
        source_manifest_hash,
        scip_python_version,
        scip_cli_version,
    )
    try:
        parser_version = importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        parser_version = "unknown"
    snapshot_id = _snapshot_identity(repo, manifest, settings, include_untracked)
    if expected_snapshot_id is not None and snapshot_id != expected_snapshot_id:
        raise RepositoryError("Repository contents changed after the ingestion job was queued")
    check_cancelled()
    developers, commits, changed_paths = extract_git_history(
        root, repo.repository_id, check_cancelled
    )
    dependency_metadata = extract_dependency_metadata(
        materialized_files,
        source_paths=[entry.path for entry in manifest if entry.path.endswith((".py", ".pyi"))],
        repository_id=repo.repository_id,
        check_cancelled=check_cancelled,
    )
    check_cancelled()
    if on_stage:
        on_stage("MATERIALIZING")
    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / snapshot_id
    if expected_runtime_materialization_id is None:
        runtime_materialization = capture_runtime_materialization(
            root,
            settings.snapshot_cache_dir,
            snapshot_id,
            include_untracked=include_untracked,
            check_cancelled=check_cancelled,
        )
    else:
        runtime_materialization = load_runtime_materialization(
            settings.snapshot_cache_dir,
            snapshot_id,
            expected_runtime_materialization_id,
        )
    blobs = cache_root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for content in materialized_files.values():
        blob = _sha(content)
        target = blobs / blob
        if not target.exists():
            temporary_blob = blobs / f"{blob}.{os.urandom(8).hex()}.tmp"
            temporary_blob.write_bytes(content)
            os.replace(temporary_blob, target)
    manifest_payload = {
        "repository": repo.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        "entries": [item.model_dump(mode="json") for item in manifest],
    }
    temp = cache_root / f"manifest.{os.urandom(8).hex()}.tmp"
    temp.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")
    os.replace(temp, cache_root / "manifest.json")

    entities: list[SourceEntity] = []
    sites: list[SourceSite] = []
    provenance: list[ProvenanceRecord] = []
    check_cancelled()
    if on_stage:
        on_stage("PARSING")
    for rel, content in sorted(sources.items()):
        check_cancelled()
        result = _parse_file(snapshot_id, rel, content, parser_version)
        entities.extend(result[0])
        sites.extend(result[1])
        provenance.extend(result[2])
        diagnostics.extend(result[3])

    analysis = SnapshotAnalysis(
        repository=repo,
        snapshot_id=snapshot_id,
        runtime_materialization_id=runtime_materialization.materialization_id,
        status=SnapshotStatus.VALIDATING,
        manifest=manifest,
        entities=entities,
        retrieval_chunks=[],
        sites=sites,
        provenance=provenance,
        developers=developers,
        commits=commits,
        changed_paths=changed_paths,
        dependency_metadata=dependency_metadata,
        diagnostics=diagnostics,
        counts={
            "files": len(manifest),
            "analyzed_python_files": len(sources),
            "entities": len(entities),
            "sites": len(sites),
            "provenance_records": len(provenance),
            "developers": len(developers),
            "commits": len(commits),
            "changed_paths": len(changed_paths),
            "dependencies": len(dependency_metadata.requirements),
            "runtime_materialized_files": len(runtime_materialization.files),
        },
        semantic_indexing="UNAVAILABLE",
        analysis_environment=environment,
    )
    document_contents = {
        path: content
        for path, content in materialized_files.items()
        if Path(path).suffix.lower() in DOCUMENT_SUFFIXES
    }
    analysis.retrieval_chunks = [
        RetrievalChunk.model_validate(item.model_dump())
        for item in build_source_chunks(analysis, sources)
    ] + build_document_chunks(snapshot_id, document_contents)
    analysis.counts["retrieval_chunks"] = len(analysis.retrieval_chunks)
    check_cancelled()
    if on_stage:
        on_stage("SEMANTIC_INDEXING")
    index, scip_version, scip_diagnostics = run_scip_index(
        analysis, settings, cancellation_event=cancellation_event
    )
    check_cancelled()
    analysis_diagnostics = [*analysis.diagnostics, *scip_diagnostics]
    bindings: list[SemanticBinding] = []
    external_symbols: list[ExternalSymbol] = []
    semantic_provenance: list[ProvenanceRecord] = []
    if (
        index is not None
        and scip_version is not None
        and scip_version == environment.scip_python_version
    ):
        bindings, external_symbols, semantic_provenance, semantic_diagnostics = join_bindings(
            analysis,
            sources,
            index,
            environment_id=environment.environment_id,
            extractor_version=scip_version,
        )
        analysis_diagnostics.extend(semantic_diagnostics)
        analysis_semantic_status = "INDEXED"
    elif index is not None and scip_version != environment.scip_python_version:
        analysis_diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.RESOLVER_ERROR,
                message="SCIP tool version changed during analysis; bindings were withheld.",
            )
        )
        analysis_semantic_status = "FAILED"
    else:
        analysis_semantic_status = (
            "FAILED"
            if any(item.code == DiagnosticCode.RESOLVER_ERROR for item in scip_diagnostics)
            else "UNAVAILABLE"
        )
    analysis = analysis.model_copy(
        update={
            "diagnostics": analysis_diagnostics,
            "provenance": [*analysis.provenance, *semantic_provenance],
            "bindings": bindings,
            "external_symbols": external_symbols,
            "semantic_indexing": analysis_semantic_status,
        }
    )
    import_edges, import_provenance, import_diagnostics = build_import_relationships(
        analysis, sources, external_symbols
    )
    analysis = analysis.model_copy(
        update={
            "imports": import_edges,
            "provenance": [*analysis.provenance, *import_provenance],
            "diagnostics": [*analysis.diagnostics, *import_diagnostics],
        }
    )
    derived_edges, dispatch_states, dispatch_provenance, dispatch_diagnostics = derive_dispatch(
        analysis, sources
    )
    analysis = analysis.model_copy(
        update={
            "derived_edges": derived_edges,
            "dispatch": dispatch_states,
            "provenance": [*analysis.provenance, *dispatch_provenance],
            "diagnostics": [*analysis.diagnostics, *dispatch_diagnostics],
        }
    )
    check_cancelled()
    if on_stage:
        on_stage("VALIDATING")
    analysis_temp = cache_root / f"analysis.{os.urandom(8).hex()}.tmp"
    analysis_temp.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    os.replace(analysis_temp, cache_root / "analysis.json")
    return analysis


def _parse_analysis_file(path: str, snapshot_id: str) -> SnapshotAnalysis:
    analysis = SnapshotAnalysis.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if analysis.snapshot_id != snapshot_id:
        raise RepositoryError("Snapshot cache identity mismatch")
    return analysis


@lru_cache(maxsize=2)
def _cached_analysis(
    path: str, snapshot_id: str, version: tuple[int, int, int, int, int]
) -> SnapshotAnalysis:
    # The version participates in the key; it is intentionally not used to read the file.
    return _parse_analysis_file(path, snapshot_id)


def load_snapshot_analysis(snapshot_id: str, settings: Settings) -> SnapshotAnalysis:
    digest = snapshot_id.removeprefix("snap_")
    if not snapshot_id.startswith("snap_") or len(digest) != 64:
        raise RepositoryError("Invalid snapshot ID")
    if any(char not in "0123456789abcdef" for char in digest):
        raise RepositoryError("Invalid snapshot ID")
    root = settings.snapshot_cache_dir.expanduser().resolve() / snapshot_id
    analysis_file = root / "analysis.json"
    if not analysis_file.is_file():
        raise FileNotFoundError("Snapshot analysis is not available in the local cache")
    # Cache parsed data only for this exact file generation. Atomic replay/materialization
    # replacements change inode/ctime even when size and mtime happen to match.
    for _attempt in range(2):
        stat = analysis_file.stat()
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stat.st_size > 64 * 1024 * 1024:
            analysis = _parse_analysis_file(str(analysis_file), snapshot_id)
        else:
            analysis = _cached_analysis(str(analysis_file), snapshot_id, version)
        after = analysis_file.stat()
        if version == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            # Models and their nested lists are mutable. Never share cache-owned objects.
            return analysis.model_copy(deep=True)
    raise RepositoryError("Snapshot analysis changed while loading; retry the request")

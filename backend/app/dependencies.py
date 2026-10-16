from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .ids import stable_id
from .models import (
    DependencyMetadata,
    DependencyRequirement,
    ProjectComponent,
    ResolvedDistribution,
)

_VCS_EGG = re.compile(r"(?:git|hg|svn|bzr)\+[^\s#]+(?:#[^\s]*?egg=([^&\s]+))")
_EXTRAS = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([^\]]*)\])?(.*)$")


def _installed_metadata() -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
    distributions: dict[str, tuple[str, str]] = {}
    try:
        package_map = importlib.metadata.packages_distributions()
    except Exception:
        package_map = {}
    for distribution in importlib.metadata.distributions():
        try:
            name = distribution.metadata["Name"]
        except KeyError:
            name = None
        version = distribution.version
        if name:
            distributions[canonicalize_name(name)] = (name, version)
    reverse: dict[str, list[str]] = {}
    for import_name, names in package_map.items():
        for name in names or []:
            reverse.setdefault(canonicalize_name(name), []).append(import_name)
    return distributions, {key: sorted(set(value)) for key, value in reverse.items()}


def _requirement_parts(
    raw: str,
) -> tuple[str, str | None, str, str | None, list[str], bool, str | None]:
    try:
        requirement = Requirement(raw)
        return (
            requirement.name,
            canonicalize_name(requirement.name),
            str(requirement.specifier),
            str(requirement.marker) if requirement.marker else None,
            sorted(requirement.extras),
            True,
            None,
        )
    except InvalidRequirement as exc:
        vcs = _VCS_EGG.search(raw)
        if vcs:
            egg = vcs.group(1)
            match = _EXTRAS.fullmatch(egg)
            if match:
                name, extras, _ = match.groups()
                return (
                    name,
                    canonicalize_name(name),
                    "",
                    None,
                    sorted(item.strip() for item in (extras or "").split(",") if item.strip()),
                    True,
                    None,
                )
        return raw, None, "", None, [], False, f"Unsupported dependency declaration: {exc}"


def _read_requirements(
    files: dict[str, bytes], check_cancelled: Callable[[], None] | None
) -> tuple[list[tuple[str, str]], list[str]]:
    entries: list[tuple[str, str]] = []
    diagnostics: list[str] = []
    active: list[str] = []
    visited: set[str] = set()

    def visit(path: str) -> None:
        normalized = PurePosixPath(path)
        if normalized.is_absolute():
            diagnostics.append(f"Requirements include escapes repository root: {path}")
            return
        parts: list[str] = []
        for part in normalized.parts:
            if part == "..":
                if not parts:
                    diagnostics.append(f"Requirements include escapes repository root: {path}")
                    return
                parts.pop()
            elif part not in {"", ".", "/"}:
                parts.append(part)
        resolved = "/".join(parts)
        if not resolved:
            diagnostics.append(f"Invalid requirements include path: {path}")
            return
        if resolved in active:
            diagnostics.append(f"Requirements include cycle: {' -> '.join([*active, resolved])}")
            return
        if resolved in visited:
            return
        content = files.get(resolved)
        if content is None:
            diagnostics.append(f"Requirements include not found: {resolved}")
            return
        active.append(resolved)
        visited.add(resolved)
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeError as exc:
            diagnostics.append(f"Could not read {resolved}: {exc}")
            active.pop()
            return
        for number, original in enumerate(lines, start=1):
            if check_cancelled:
                check_cancelled()
            line = original.strip()
            if not line or line.startswith("#"):
                continue
            include = re.match(r"^(?:--requirement|-r)(?:\s+|=)?(.+)$", line)
            if include:
                visit(str(PurePosixPath(resolved).parent / include.group(1).strip().strip("\"'")))
                continue
            if line.startswith(("-c ", "--constraint ")):
                child = line.split(maxsplit=1)[1]
                visit(str(PurePosixPath(resolved).parent / child.strip("\"'")))
                continue
            if line.startswith("-"):
                diagnostics.append(
                    f"Unsupported requirements option at {resolved}:{number}: {line}"
                )
                continue
            # PEP 508 permits whitespace-prefixed comments; keep URL fragments intact.
            line = line.split(" #", 1)[0].strip()
            if line:
                entries.append((resolved, line))
        active.pop()

    roots = sorted(
        path
        for path in files
        if PurePosixPath(path).name in {"requirements.txt", "requirements-dev.txt"}
    )
    for root_file in roots:
        visit(root_file)
    return entries, diagnostics


def _is_under(path: str, root_path: str) -> bool:
    if root_path == ".":
        return True
    return path == root_path or path.startswith(f"{root_path}/")


def extract_dependency_metadata(
    files: dict[str, bytes],
    source_paths: list[str] | None = None,
    repository_id: str = "",
    check_cancelled: Callable[[], None] | None = None,
) -> DependencyMetadata:
    installed, import_map = _installed_metadata()
    diagnostics: list[str] = []
    component_specs: list[tuple[str, str, dict[str, Any]]] = []
    pyprojects = sorted(path for path in files if PurePosixPath(path).name == "pyproject.toml")
    for path in pyprojects:
        if check_cancelled:
            check_cancelled()
        try:
            config = tomllib.loads(files[path].decode("utf-8"))
            project = config.get("project", {})
            name = str(project.get("name") or PurePosixPath(path).parent.name or "project")
            component_specs.append((path, name, config))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            diagnostics.append(f"Could not parse {path}: {exc}")
            component_specs.append((path, PurePosixPath(path).parent.name or "project", {}))

    if not component_specs:
        component_specs.append(("pyproject.toml", "repository", {}))

    roots = [
        (PurePosixPath(path).parent.as_posix(), name, path, config)
        for path, name, config in component_specs
    ]
    component_ids = {
        path: stable_id(repository_id or "repository", root_path, name, prefix="component_")
        for root_path, name, path, _config in roots
    }

    def nearest_component(path: str) -> str | None:
        candidates = [
            (len(root_path.split("/")), component_ids[manifest])
            for root_path, _name, manifest, _config in roots
            if _is_under(path, root_path)
        ]
        return max(candidates)[1] if candidates else None

    requirements: list[DependencyRequirement] = []
    resolved_distributions: list[ResolvedDistribution] = []

    def add_requirement(raw: str, source: str, optional_group: str | None = None) -> None:
        if check_cancelled:
            check_cancelled()
        name, normalized, specifier, marker, extras, supported, diagnostic = _requirement_parts(raw)
        requirement_id = stable_id(source, optional_group or "", raw, prefix="dep_")
        component_id = nearest_component(source)
        requirements.append(
            DependencyRequirement(
                requirement_id=requirement_id,
                name=name,
                normalized_name=normalized,
                raw_requirement=raw,
                specifier=specifier,
                marker=marker,
                extras=extras,
                source_file=source,
                optional_group=optional_group,
                component_id=component_id,
                supported=supported,
                diagnostic=diagnostic,
            )
        )
        if diagnostic:
            diagnostics.append(f"{source}: {diagnostic}")
        if normalized and normalized in installed:
            distribution_name, version = installed[normalized]
            resolved_distributions.append(
                ResolvedDistribution(
                    distribution_id=stable_id(
                        requirement_id, distribution_name, version, prefix="dist_"
                    ),
                    requirement_id=requirement_id,
                    distribution_name=distribution_name,
                    version=version,
                    import_names=import_map.get(normalized, []),
                )
            )

    for path, _name, config in component_specs:
        project = config.get("project", {})
        for raw in project.get("dependencies", []):
            add_requirement(raw, path)
        for raw in config.get("build-system", {}).get("requires", []):
            add_requirement(raw, path, "build-system")
        for group, items in sorted(project.get("optional-dependencies", {}).items()):
            for raw in items:
                add_requirement(raw, path, group)
        if "dependencies" in project.get("dynamic", []):
            diagnostics.append(
                f"{path} declares dependencies as dynamic; static extraction is incomplete"
            )

    requirement_entries, requirement_diagnostics = _read_requirements(files, check_cancelled)
    diagnostics.extend(requirement_diagnostics)
    for source, raw in requirement_entries:
        add_requirement(raw, source)

    for path in sorted(item for item in files if PurePosixPath(item).name == "setup.py"):
        component_id = nearest_component(path)
        requirement_id = stable_id(path, "unsupported", prefix="dep_")
        requirements.append(
            DependencyRequirement(
                requirement_id=requirement_id,
                name="setup.py dependencies",
                raw_requirement="",
                source_file=path,
                component_id=component_id,
                supported=False,
                diagnostic=(
                    "UNSUPPORTED_V1_DYNAMIC_DEPENDENCY_METADATA: setup.py was not executed."
                ),
            )
        )
        diagnostics.append(f"{path} dependency metadata is unsupported; setup.py was not executed")

    manifest_files_by_component: dict[str, set[str]] = {
        item: set() for item in component_ids.values()
    }
    source_paths_by_component: dict[str, set[str]] = {
        item: set() for item in component_ids.values()
    }
    requirement_ids_by_component: dict[str, list[str]] = {
        item: [] for item in component_ids.values()
    }
    for path in pyprojects:
        manifest_files_by_component[component_ids[path]].add(path)
    for requirement in requirements:
        if requirement.component_id:
            manifest_files_by_component[requirement.component_id].add(requirement.source_file)
            requirement_ids_by_component[requirement.component_id].append(
                requirement.requirement_id
            )
        elif requirement.source_file:
            diagnostics.append(
                f"Dependency manifest {requirement.source_file} is outside configured project roots"
            )
    for source in source_paths or []:
        component_id = nearest_component(source)
        if component_id:
            source_paths_by_component[component_id].add(source)

    components = []
    for root_path, name, manifest, _config in roots:
        component_id = component_ids[manifest]
        components.append(
            ProjectComponent(
                component_id=component_id,
                name=name,
                root_path=root_path,
                manifest_files=sorted(manifest_files_by_component[component_id]),
                source_paths=sorted(source_paths_by_component[component_id]),
                dependency_requirement_ids=sorted(requirement_ids_by_component[component_id]),
            )
        )

    return DependencyMetadata(
        requirements=sorted(
            requirements,
            key=lambda item: (item.source_file, item.optional_group or "", item.requirement_id),
        ),
        resolved_distributions=sorted(
            resolved_distributions, key=lambda item: (item.requirement_id, item.distribution_id)
        ),
        components=components,
        diagnostics=diagnostics,
    )

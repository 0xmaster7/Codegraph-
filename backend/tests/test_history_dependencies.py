from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app import dependencies
from backend.app.config import Settings
from backend.app.dependencies import extract_dependency_metadata
from backend.app.history import extract_git_history
from backend.app.ids import stable_id
from backend.app.ingestion import analyze_repository, inspect_repository
from backend.app.models import GitChangeStatus
from backend.tests.test_ground_truth_fixture import (
    FIXTURE,
    GENERATED_FIXTURE_FILES,
    _create_history,
)


def test_history_extracts_mailmapped_developers_renames_deletions_and_parent_diffs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "history"
    expected = _create_history(repo)
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    _, repository = inspect_repository(repo, Settings(allowed_repository_roots=[tmp_path]))

    developers, commits, changes = extract_git_history(repo, repository.repository_id)

    assert {commit.commit_sha for commit in commits} == set(expected.values())
    assert all(
        commit.commit_id == f"{repository.repository_id}::commit::{commit.commit_sha}"
        for commit in commits
    )
    assert expected == manifest["history"]["expected_commits"]
    assert len(commits) == 4
    assert any(developer.name == "CodeGraph Test" for developer in developers)
    developer = next(item for item in developers if item.name == "CodeGraph Test")
    assert developer.developer_id == stable_id(
        developer.name.casefold(), developer.email.casefold()
    )
    rename_delete = f"{repository.repository_id}::commit::{expected['rename-delete']}"
    deleted = [
        change
        for change in changes
        if change.commit_id == rename_delete and change.status == GitChangeStatus.DELETED
    ]
    renamed = [
        change
        for change in changes
        if change.commit_id == rename_delete and change.status == GitChangeStatus.RENAMED
    ]
    assert [(item.old_path, item.new_path) for item in deleted] == [("pkg/obsolete.py", None)]
    assert [(item.old_path, item.new_path, item.similarity) for item in renamed] == [
        ("pkg/legacy.py", "pkg/current.py", 100)
    ]

    merge_id = f"{repository.repository_id}::commit::{expected['merge']}"
    merge = next(commit for commit in commits if commit.commit_id == merge_id)
    assert len(merge.parent_commit_ids) == 2
    assert merge.parent_commit_ids == [
        f"{repository.repository_id}::commit::{sha}" for sha in merge.parent_commit_shas
    ]
    merge_changes = [item for item in changes if item.commit_id == merge.commit_id]
    assert {item.parent_index for item in merge_changes} == {0, 1}
    assert {item.parent_commit_sha for item in merge_changes} == set(merge.parent_commit_shas)

    checks = 0

    def cancel_after_first_commit() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        extract_git_history(repo, repository.repository_id, cancel_after_first_commit)


def test_shallow_history_keeps_partial_boundary_and_detached_head_facts(tmp_path: Path) -> None:
    full = tmp_path / "full"
    expected = _create_history(full)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", full.as_uri(), str(shallow)], check=True)
    _, shallow_info = inspect_repository(shallow, Settings(allowed_repository_roots=[tmp_path]))
    developers, commits, _ = extract_git_history(shallow, shallow_info.repository_id)
    assert developers
    assert shallow_info.is_shallow
    assert [item.commit_sha for item in commits] == [expected["merge"]]
    assert len(commits[0].parent_commit_ids) == 2

    subprocess.run(["git", "-C", str(shallow), "checkout", "--detach", "HEAD"], check=True)
    _, detached_info = inspect_repository(shallow, Settings(allowed_repository_roots=[tmp_path]))
    assert detached_info.is_detached


def test_requirements_include_markers_extras_cycle_and_setup_py_are_static(
    tmp_path: Path,
    monkeypatch,
) -> None:
    files = {
        "pyproject.toml": b"""
[project]
name = 'sample'
dependencies = ['requests>=2']
[project.optional-dependencies]
test = ["typing-extensions[testing]>=4; python_version >= '3.11'"]
""",
        "requirements.txt": b"-r requirements/base.txt\n",
        "requirements/base.txt": (
            b"requests>=2.31 # included dependency\n-r ../requirements.txt\n"
            b"git+https://example.test/demo.git@v1#egg=DemoPkg[fast]\n"
        ),
        "setup.py": b"from pathlib import Path\nPath('executed').touch()\n",
    }
    monkeypatch.setattr(
        dependencies.importlib.metadata,
        "packages_distributions",
        lambda: {"yaml": ["PyYAML"]},
    )

    class Distribution:
        metadata = {"Name": "PyYAML"}
        version = "6.0"

    monkeypatch.setattr(dependencies.importlib.metadata, "distributions", lambda: [Distribution()])
    files["requirements/base.txt"] += b"PyYAML>=6\n"
    result = extract_dependency_metadata(files)

    by_source_name = {(item.source_file, item.name): item for item in result.requirements}
    requests = by_source_name[("pyproject.toml", "requests")]
    assert requests.raw_requirement == "requests>=2"
    assert requests.normalized_name == "requests"
    assert all(
        item.requirement_id != requests.requirement_id for item in result.resolved_distributions
    )
    optional = next(item for item in result.requirements if item.optional_group == "test")
    assert optional.extras == ["testing"]
    assert optional.marker == 'python_version >= "3.11"'
    yaml_requirement = by_source_name[("requirements/base.txt", "PyYAML")]
    yaml_distribution = next(
        item
        for item in result.resolved_distributions
        if item.requirement_id == yaml_requirement.requirement_id
    )
    assert yaml_distribution.import_names == ["yaml"]
    assert yaml_distribution.version == "6.0"
    vcs = next(item for item in result.requirements if item.name == "DemoPkg")
    assert vcs.raw_requirement == "git+https://example.test/demo.git@v1#egg=DemoPkg[fast]"
    assert vcs.normalized_name == "demopkg"
    assert vcs.extras == ["fast"]
    assert any("include cycle" in item for item in result.diagnostics)
    assert any("setup.py dependency metadata is unsupported" in item for item in result.diagnostics)
    assert not (tmp_path / "executed").exists()


def test_dependency_extraction_uses_materialized_requirement_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(
        FIXTURE,
        repo,
        ignore=shutil.ignore_patterns("ground_truth.json", *GENERATED_FIXTURE_FILES),
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    analysis = analyze_repository(
        repo,
        Settings(
            allowed_repository_roots=[tmp_path],
            snapshot_cache_dir=tmp_path / "cache",
            scip_python_bin="",
            scip_cli_bin="",
        ),
    )
    assert {"requirements.txt", "requirements/base.txt", "pyproject.toml"} <= {
        entry.path for entry in analysis.manifest
    }
    assert any(
        item.name == "requests" and item.source_file == "pyproject.toml"
        for item in analysis.dependency_metadata.requirements
    )
    assert any(
        item.name == "setuptools"
        and item.source_file == "pyproject.toml"
        and item.optional_group == "build-system"
        for item in analysis.dependency_metadata.requirements
    )
    assert analysis.counts["commits"] == 1
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    actual = {
        (item.name, item.source_file, item.specifier, item.marker, item.optional_group)
        for item in analysis.dependency_metadata.requirements
    }
    for expected in manifest["expected_dependencies"]:
        assert any(
            name == expected["name"]
            and source == expected["source_file"]
            and ("optional_group" not in expected or group == expected["optional_group"])
            and ("specifier" not in expected or specifier == expected["specifier"])
            and ("marker" not in expected or marker == expected["marker"])
            for name, source, specifier, marker, group in actual
        ), expected


def test_monorepo_dependencies_and_sources_stay_in_nearest_components(
    monkeypatch,
) -> None:
    files = {
        "packages/alpha/pyproject.toml": (
            b"[project]\nname='alpha'\ndependencies=['alpha-dep>=1']\n"
        ),
        "packages/alpha/src/a.py": b"",
        "packages/beta/pyproject.toml": b"[project]\nname='beta'\ndependencies=['beta-dep>=2']\n",
        "packages/beta/src/b.py": b"",
        "shared.py": b"",
    }
    monkeypatch.setattr(dependencies.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(dependencies.importlib.metadata, "packages_distributions", lambda: {})

    result = extract_dependency_metadata(
        files,
        source_paths=["packages/alpha/src/a.py", "packages/beta/src/b.py", "shared.py"],
        repository_id="repo_fixture",
    )

    components = {item.name: item for item in result.components}
    requirements = {item.name: item for item in result.requirements}
    assert components["alpha"].source_paths == ["packages/alpha/src/a.py"]
    assert components["beta"].source_paths == ["packages/beta/src/b.py"]
    assert not any("shared.py" in item.source_paths for item in components.values())
    assert requirements["alpha-dep"].component_id == components["alpha"].component_id
    assert requirements["beta-dep"].component_id == components["beta"].component_id

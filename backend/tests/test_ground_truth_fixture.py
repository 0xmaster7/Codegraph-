from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from backend.app.config import Settings
from backend.app.materialization import capture_runtime_materialization, runtime_files_root
from backend.app.models import RuntimeRunStatus
from backend.app.test_execution import DockerTestExecutor

FIXTURE = Path(__file__).parent / "fixtures" / "python_ground_truth_repo"
GENERATED_FIXTURE_FILES = ("__pycache__", "*.py[cod]", ".pytest_cache")


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


def _commit(root: Path, message: str, timestamp: str) -> str:
    stamp = f"{timestamp} +0000"
    env = {
        "GIT_AUTHOR_NAME": "CodeGraph Fixture",
        "GIT_AUTHOR_EMAIL": "fixture-author@example.test",
        "GIT_COMMITTER_NAME": "CodeGraph Fixture",
        "GIT_COMMITTER_EMAIL": "fixture-author@example.test",
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message, env=env)
    return _git(root, "rev-parse", "HEAD")


def _create_history(root: Path) -> dict[str, str]:
    shutil.copytree(
        FIXTURE,
        root,
        ignore=shutil.ignore_patterns("ground_truth.json", *GENERATED_FIXTURE_FILES),
    )
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    _git(root, "config", "user.name", "CodeGraph Fixture")
    _git(root, "config", "user.email", "fixture-author@example.test")
    history = {"initial": _commit(root, "fixture: initial source corpus", "2020-01-01T00:00:00")}

    _git(root, "switch", "-c", "rename-delete")
    _git(root, "mv", "pkg/legacy.py", "pkg/current.py")
    _git(root, "rm", "pkg/obsolete.py")
    history["rename-delete"] = _commit(
        root, "fixture: rename and delete paths", "2020-01-02T00:00:00"
    )

    _git(root, "switch", "main")
    (root / "fixture-mainline.txt").write_text("mainline\n", encoding="utf-8")
    history["mainline"] = _commit(root, "fixture: add mainline marker", "2020-01-03T00:00:00")

    _git(
        root,
        "merge",
        "--no-ff",
        "rename-delete",
        "-m",
        "fixture: merge rename branch",
        env={
            "GIT_AUTHOR_NAME": "CodeGraph Fixture",
            "GIT_AUTHOR_EMAIL": "fixture-author@example.test",
            "GIT_COMMITTER_NAME": "CodeGraph Fixture",
            "GIT_COMMITTER_EMAIL": "fixture-author@example.test",
            "GIT_AUTHOR_DATE": "2020-01-04T00:00:00 +0000",
            "GIT_COMMITTER_DATE": "2020-01-04T00:00:00 +0000",
        },
    )
    history["merge"] = _git(root, "rev-parse", "HEAD")
    return history


def test_ground_truth_manifest_covers_every_planned_fixture_case() -> None:
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    required = set(manifest["required_cases"])
    represented = {
        "top_level_function": "def verify_token" in (FIXTURE / "pkg/auth.py").read_text(),
        "nested_function": "def inner" in (FIXTURE / "pkg/checkout.py").read_text(),
        "lambda": "lambda value" in (FIXTURE / "pkg/checkout.py").read_text(),
        "class": "class Base" in (FIXTURE / "pkg/checkout.py").read_text(),
        "method": "def validate" in (FIXTURE / "pkg/checkout.py").read_text(),
        "property": "@property" in (FIXTURE / "pkg/checkout.py").read_text(),
        "staticmethod": "@staticmethod" in (FIXTURE / "pkg/checkout.py").read_text(),
        "classmethod": "@classmethod" in (FIXTURE / "pkg/checkout.py").read_text(),
        "same_name_two_modules": all(
            "def normalize" in (FIXTURE / f"pkg/{name}.py").read_text()
            for name in ("common", "other")
        ),
        "overload": "@overload" in (FIXTURE / "pkg/models.py").read_text(),
        "conditional_redefinition": "conditional_value =" in (FIXTURE / "pkg/auth.py").read_text(),
        "plain_import": "import os" in (FIXTURE / "pkg/imports.py").read_text(),
        "from_import": "from .common import normalize" in (FIXTURE / "pkg/imports.py").read_text(),
        "alias": "common as common_alias" in (FIXTURE / "pkg/imports.py").read_text(),
        "relative_import": "from .auth" in (FIXTURE / "pkg/reexports.py").read_text(),
        "re_export": "__all__" in (FIXTURE / "pkg/reexports.py").read_text(),
        "type_checking_import": "if TYPE_CHECKING" in (FIXTURE / "pkg/imports.py").read_text(),
        "conditional_import": "if os.name" in (FIXTURE / "pkg/imports.py").read_text(),
        "dynamic_import": "import_module(module_name)" in (FIXTURE / "pkg/imports.py").read_text(),
        "direct_call": "verify_token(value)" in (FIXTURE / "pkg/checkout.py").read_text(),
        "aliased_call": "check(value)" in (FIXTURE / "pkg/checkout.py").read_text(),
        "method_call": "child.run()" in (FIXTURE / "pkg/base_dispatch.py").read_text(),
        "constructor_call": "StrictCheckout()" in (FIXTURE / "pkg/checkout.py").read_text(),
        "callback_value": "fn=callback" in (FIXTURE / "pkg/checkout.py").read_text(),
        "module_level_call": "MODULE_CHECKOUT = StrictCheckout.from_value"
        in (FIXTURE / "pkg/checkout.py").read_text(),
        "class_body_call": "default = StrictCheckout.from_value"
        in (FIXTURE / "pkg/checkout.py").read_text(),
        "dynamic_getattr": "getattr(target, name)()" in (FIXTURE / "pkg/auth.py").read_text(),
        "monkeypatch": "monkeypatch.setattr" in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "decorator_reference": "@property" in (FIXTURE / "pkg/checkout.py").read_text(),
        "simple_inheritance": "class StrictCheckout(Base)"
        in (FIXTURE / "pkg/checkout.py").read_text(),
        "override": "class StrictCheckout(Base)" in (FIXTURE / "pkg/checkout.py").read_text(),
        "two_overrides": "class FastCheckout(Base)" in (FIXTURE / "pkg/checkout.py").read_text(),
        "multiple_inheritance_mro": "class Child(Left, Right)"
        in (FIXTURE / "pkg/base_dispatch.py").read_text(),
        "external_base": "class UnresolvedCheckout(UnknownExternalBase)"
        in (FIXTURE / "pkg/checkout.py").read_text(),
        "emoji": "🚀" in (FIXTURE / "pkg/checkout.py").read_text(),
        "accented_text": "café" in (FIXTURE / "pkg/checkout.py").read_text(),
        "unicode_identifier": "café λ" in (FIXTURE / "pkg/checkout.py").read_text(),
        "pytest_function": "def test_verifies_token"
        in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "parametrized_test": "@pytest.mark.parametrize"
        in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "pytest_fixture": "@pytest.fixture" in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "duplicate_test_basename": (FIXTURE / "tests/unit/test_flow.py").exists()
        and (FIXTURE / "tests/integration/test_flow.py").exists(),
        "import_without_call": "import pkg.unvisited"
        in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "threaded_call": "ThreadPoolExecutor" in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "optional_subprocess_expected_partial": "expected_partial"
        in (FIXTURE / "tests/unit/test_flow.py").read_text(),
        "multiple_commits": True,
        "rename": True,
        "deletion": True,
        "merge_commit": True,
        "mailmap": (FIXTURE / ".mailmap").exists(),
        "shallow_clone": True,
        "pyproject_dependencies": "dependencies" in (FIXTURE / "pyproject.toml").read_text(),
        "requirements_include": "-r requirements/base.txt"
        in (FIXTURE / "requirements.txt").read_text(),
        "extras_and_markers": "optional-dependencies" in (FIXTURE / "pyproject.toml").read_text()
        and ";" in (FIXTURE / "pyproject.toml").read_text(),
        "distribution_import_name_mismatch": "Pillow" in (FIXTURE / "pyproject.toml").read_text()
        and "from PIL import Image" in (FIXTURE / "pkg/image_support.py").read_text(),
    }
    assert required == represented.keys()
    assert all(represented.values()), [case for case, exists in represented.items() if not exists]

    fixture_files = [path for path in FIXTURE.rglob("*.py")]
    for item in manifest["expected_semantics"]:
        source = (FIXTURE / item["path"]).read_bytes()
        source_span = item["span"]
        assert source[source_span["start_byte"] : source_span["end_byte"]] == item[
            "snippet"
        ].encode("utf-8"), item["id"]
        assert item["id"]
    ids = [item["id"] for item in manifest["expected_semantics"]]
    assert len(ids) == len(set(ids))
    assert fixture_files
    for sample in manifest["unicode_samples"]:
        assert sample["text"].encode("utf-8") in (FIXTURE / sample["path"]).read_bytes()


def test_fixture_tests_pass_with_duplicate_basename_importlib_collection(
    tmp_path: Path, runtime_image: str
) -> None:
    repo = tmp_path / "fixture"
    shutil.copytree(
        FIXTURE,
        repo,
        ignore=shutil.ignore_patterns("ground_truth.json", ".mailmap", *GENERATED_FIXTURE_FILES),
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    snapshot_id = "snap_" + "e" * 64
    materialization = capture_runtime_materialization(repo, tmp_path / "cache", snapshot_id)
    snapshot_root = runtime_files_root(
        tmp_path / "cache", snapshot_id, materialization.materialization_id
    )
    executor = DockerTestExecutor(Settings(runtime_container_image=runtime_image))
    runtime_environment_id = "runtime_ground_truth_fixture"
    collection = executor.collect(snapshot_root, snapshot_id, runtime_environment_id)
    assert collection.status == RuntimeRunStatus.COMPLETED, collection.stdout + collection.stderr
    remaining = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=codegraph-test-"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not remaining.stdout.strip()
    result = executor.execute(
        snapshot_root,
        snapshot_id,
        runtime_environment_id,
        collection.collected_node_ids,
        "run_ground_truth_fixture",
    )
    assert result.status == RuntimeRunStatus.COMPLETED, result.stdout + result.stderr
    assert result.passed == 9


def test_fixture_history_has_frozen_events_merge_rename_delete_and_shallow_clone(
    tmp_path: Path,
) -> None:
    manifest = json.loads((FIXTURE / "ground_truth.json").read_text(encoding="utf-8"))
    repo = tmp_path / "history"
    history = _create_history(repo)

    assert list(history) == manifest["history"]["expected_events"]
    assert len(history) == manifest["history"]["expected_commit_count"]
    assert history == manifest["history"]["expected_commits"]
    assert all(
        _git(repo, "cat-file", "-e", f"{commit}^{{commit}}") == "" for commit in history.values()
    )
    name_status = _git(repo, "show", "--pretty=format:", "--name-status", history["rename-delete"])
    assert set(name_status.splitlines()) == {
        f"R100\t{manifest['history']['renamed_from']}\t{manifest['history']['renamed_to']}",
        f"D\t{manifest['history']['deleted_path']}",
    }
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", history["merge"]).split()) == 3
    assert (repo / ".mailmap").exists()

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", Path(repo).as_uri(), str(shallow)],
        check=True,
    )
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(shallow, "rev-parse", "HEAD") == history["merge"]

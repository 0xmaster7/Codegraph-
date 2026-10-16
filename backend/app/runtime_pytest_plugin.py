"""Small CodeGraph pytest plugin mounted read-only into isolated runtime containers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path

import pytest

_CURRENT_INVOCATION: ContextVar[str | None] = ContextVar("codegraph_test_invocation", default=None)
_ENTRIES: list[dict[str, object]] = []
_CALLS: list[dict[str, object]] = []
_DIAGNOSTICS: dict[tuple[str, str, int, str], dict[str, object]] = {}
_BASE_PROFILE = None
_BASE_THREAD_PROFILE = None


def pytest_collection_finish(session) -> None:
    session.config._codegraph_node_ids = [item.nodeid for item in session.items]


def pytest_configure(config) -> None:
    packages = sorted(
        f"{distribution.metadata['Name']}=={distribution.version}"
        for distribution in importlib.metadata.distributions()
    )
    config._codegraph_environment = {
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "python_executable_hash": hashlib.sha256(sys.executable.encode()).hexdigest(),
        "sys_path_hash": hashlib.sha256("\0".join(sys.path).encode()).hexdigest(),
        "installed_distributions_hash": hashlib.sha256("\n".join(packages).encode()).hexdigest(),
    }
    try:
        source_map_path = os.environ["CODEGRAPH_SOURCE_MAP_PATH"]
        source_map = json.loads(Path(source_map_path).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError):
        source_map = {}
    config._codegraph_entities = source_map.get("entities", [])
    config._codegraph_invocations = source_map.get("invocations", {})
    config._codegraph_sites = source_map.get("sites", [])
    config._codegraph_root = Path("/workspace").resolve()
    global _BASE_PROFILE, _BASE_THREAD_PROFILE
    _BASE_PROFILE = _profile(config, None)
    _BASE_THREAD_PROFILE = _profile(config, None)
    sys.setprofile(_BASE_PROFILE)
    threading.setprofile(_BASE_THREAD_PROFILE)


def pytest_unconfigure(config) -> None:
    """Remove tracing before interpreter shutdown clears module globals."""
    del config
    sys.setprofile(None)
    threading.setprofile(None)


def _profile(config, invocation_id: str | None):
    def resolve_entity(code):
        try:
            filename = code.co_filename
            repository_prefix = f"{config._codegraph_root}{os.sep}"
            if not filename.startswith(repository_prefix):
                return None, None, None
            candidate_path = Path(filename)
            source_path = candidate_path.resolve()
            relative_path = source_path.relative_to(config._codegraph_root).as_posix()
        except (OSError, ValueError):
            return None, None, None
        qualified_name = code.co_qualname
        matches = [
            entity
            for entity in config._codegraph_entities
            if entity.get("file_path") == relative_path
            and entity.get("kind") in {"Function", "Method"}
            and (
                entity.get("qualified_name") == qualified_name
                or str(entity.get("qualified_name", "")).endswith(f".{qualified_name}")
            )
        ]
        return relative_path, qualified_name, matches

    def resolve_site(caller, caller_entity_id: str):
        caller_path, _caller_name, _matches = resolve_entity(caller.f_code)
        if caller_path is None:
            return None, None, None
        candidates = [
            site
            for site in config._codegraph_sites
            if site.get("file_path") == caller_path
            and site.get("context_id") == caller_entity_id
            and int(site.get("start_line", 0)) <= caller.f_lineno <= int(site.get("end_line", 0))
        ]
        return caller_path, caller.f_lineno, candidates

    def record(frame, event: str, _arg: object) -> None:
        if event != "call" or frame.f_code.co_name == "<module>":
            return
        relative_path, qualified_name, matches = resolve_entity(frame.f_code)
        if relative_path is None or qualified_name is None or matches is None:
            return
        current = invocation_id or _CURRENT_INVOCATION.get()
        if len(matches) == 1:
            caller = frame.f_back
            if caller is not None:
                caller_path, caller_name, caller_matches = resolve_entity(caller.f_code)
                if caller_matches and len(caller_matches) == 1:
                    site_path, caller_line, site_matches = resolve_site(
                        caller, caller_matches[0]["entity_id"]
                    )
                    site_id = (
                        site_matches[0]["site_id"]
                        if site_matches is not None and len(site_matches) == 1
                        else None
                    )
                    _CALLS.append(
                        {
                            "source_entity_id": caller_matches[0]["entity_id"],
                            "target_entity_id": matches[0]["entity_id"],
                            "site_id": site_id,
                            "file_path": site_path,
                            "qualified_name": caller_name,
                            "first_line": caller_line,
                            "test_invocation_id": current,
                            "context": "TEST_INVOCATION" if current else "COLLECTION_OR_SESSION",
                            "observed_at": time.time(),
                        }
                    )
                elif caller.f_code.co_filename.startswith("/workspace/"):
                    _record_diagnostic(
                        "UNMAPPED_RUNTIME_CALLSITE",
                        caller.f_code.co_filename.removeprefix("/workspace/"),
                        caller.f_lineno,
                        caller.f_code.co_qualname,
                        "Runtime caller did not map to one indexed function or method.",
                    )
            _ENTRIES.append(
                {
                    "entity_id": matches[0]["entity_id"],
                    "file_path": relative_path,
                    "qualified_name": qualified_name,
                    "first_line": frame.f_code.co_firstlineno,
                    "test_invocation_id": current,
                    "context": "TEST_INVOCATION" if current else "COLLECTION_OR_SESSION",
                    "observed_at": time.time(),
                }
            )
        else:
            _record_diagnostic(
                "UNMAPPED_RUNTIME_FUNCTION",
                relative_path,
                frame.f_code.co_firstlineno,
                qualified_name,
                "Runtime function entry could not be mapped to a source function or method.",
            )

    return record


def _record_diagnostic(
    code: str, file_path: str, first_line: int, qualified_name: str, message: str
) -> None:
    signature = (code, file_path, first_line, qualified_name)
    _DIAGNOSTICS.setdefault(
        signature,
        {
            "code": code,
            "message": message,
            "file_path": file_path,
            "qualified_name": qualified_name,
            "first_line": first_line,
        },
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    config = item.config
    node_id = item.nodeid
    invocation_id = config._codegraph_invocations.get(node_id)
    token = _CURRENT_INVOCATION.set(invocation_id)
    sys.setprofile(_profile(config, invocation_id))
    threading.setprofile(_profile(config, invocation_id))
    try:
        yield
    finally:
        _CURRENT_INVOCATION.reset(token)
        sys.setprofile(_BASE_PROFILE)
        threading.setprofile(_BASE_THREAD_PROFILE)


def pytest_sessionfinish(session, exitstatus) -> None:
    path = os.environ.get("CODEGRAPH_PLUGIN_RESULT_PATH")
    if not path:
        return
    stats = session.config.pluginmanager.get_plugin("terminalreporter")
    counts = {name: 0 for name in ("passed", "failed", "skipped")}
    if stats is not None:
        for name in counts:
            counts[name] = len(stats.stats.get(name, []))
    result = {
        "exit_code": int(exitstatus),
        "node_ids": list(getattr(session.config, "_codegraph_node_ids", [])),
        "environment": getattr(session.config, "_codegraph_environment", {}),
        "runtime_entries": _ENTRIES,
        "runtime_calls": _CALLS,
        "runtime_diagnostics": list(_DIAGNOSTICS.values()),
        **counts,
    }
    with open(path, "w", encoding="utf-8") as output:
        json.dump(result, output, sort_keys=True)

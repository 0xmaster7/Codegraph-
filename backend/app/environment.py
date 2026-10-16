from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import shutil
import subprocess
import sys

from .ids import stable_id
from .models import AnalysisEnvironment


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _node_version() -> str:
    node = shutil.which("node")
    if not node:
        return "unavailable"
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip()[:80] or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def capture_environment(
    source_manifest_hash: str,
    scip_python_version: str,
    scip_cli_version: str,
) -> AnalysisEnvironment:
    distributions = sorted(
        f"{dist.name}=={dist.version}" for dist in importlib.metadata.distributions()
    )
    executable_hash = _hash(sys.executable)
    sys_path_hash = _hash("\n".join(sys.path))
    distributions_hash = _hash("\n".join(distributions))
    node_version = _node_version()
    python_version = platform.python_version()
    platform_name = platform.platform()
    environment_id = stable_id(
        python_version,
        platform_name,
        executable_hash,
        sys_path_hash,
        distributions_hash,
        scip_python_version,
        scip_cli_version,
        node_version,
        source_manifest_hash,
        prefix="env_",
    )
    return AnalysisEnvironment(
        environment_id=environment_id,
        python_version=python_version,
        platform=platform_name,
        python_executable_hash=executable_hash,
        sys_path_hash=sys_path_hash,
        installed_distributions_hash=distributions_hash,
        scip_python_version=scip_python_version,
        scip_cli_version=scip_cli_version,
        node_version=node_version,
        source_manifest_hash=source_manifest_hash,
    )

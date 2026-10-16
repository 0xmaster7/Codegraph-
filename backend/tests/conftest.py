from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def runtime_image() -> str:
    available = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=10, check=False
    )
    if available.returncode:
        pytest.skip("Docker daemon is unavailable for real-container integration tests")
    dockerfile = Path(__file__).parent / "docker" / "runtime.Dockerfile"
    tag = "codegraph-m12-runtime-test:python311-pytest84"
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        subprocess.run(
            [
                "docker",
                "build",
                "--quiet",
                "-f",
                str(dockerfile),
                "-t",
                tag,
                str(dockerfile.parent),
            ],
            check=True,
            timeout=300,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            check=True,
            capture_output=True,
            text=True,
        )
    image_id = result.stdout.strip()
    yield image_id

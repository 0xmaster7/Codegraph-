"""CodeGraph-managed container-side supervisor for isolated pytest child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

_RESULT_PREFIX = "CODEGRAPH_RUNTIME_RESULT:"


def _drain(stream, limit: int, output: bytearray) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = limit - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
    finally:
        stream.close()


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        raise SystemExit("usage: runtime_supervisor.py -- <typed pytest argv>")
    try:
        output_limit = int(os.environ["CODEGRAPH_OUTPUT_LIMIT_BYTES"])
        if output_limit < 1024:
            raise ValueError
    except (KeyError, ValueError):
        raise SystemExit("invalid runtime output limit") from None

    child_environment = os.environ.copy()
    child_environment["CODEGRAPH_PLUGIN_RESULT_PATH"] = "/tmp/codegraph-plugin-result.json"
    child_environment.pop("CODEGRAPH_RESULT_PATH", None)
    child = subprocess.Popen(
        [sys.executable, *sys.argv[2:]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment,
        user=65532,
        group=65532,
    )
    stdout = bytearray()
    stderr = bytearray()
    stdout_thread = threading.Thread(
        target=_drain, args=(child.stdout, output_limit, stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain, args=(child.stderr, output_limit, stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    exit_code = child.wait()
    stdout_thread.join()
    stderr_thread.join()

    plugin_result: dict[str, object] = {}
    result_path = Path(child_environment["CODEGRAPH_PLUGIN_RESULT_PATH"])
    try:
        if result_path.stat().st_size <= output_limit:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plugin_result = loaded
    except (OSError, json.JSONDecodeError):
        pass

    payload = {
        "exit_code": exit_code,
        "plugin_result": plugin_result,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > output_limit:
        serialized = json.dumps(
            {"exit_code": exit_code, "error": "runtime result exceeds output limit"},
            sort_keys=True,
            separators=(",", ":"),
        )
    sys.stdout.write(f"{_RESULT_PREFIX}{serialized}\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

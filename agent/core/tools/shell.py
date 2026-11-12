"""Shell and Python execution tools."""

import os
import subprocess
import sys
from typing import Any, Dict

from core.config import WORKSPACE_DIR
from core.tools import tool, ToolContext

# Without a timeout a runaway snippet hangs the whole turn with no output and
# no way to recover - a loop issuing one HTTP request per item will happily run
# for hours. Killing it returns a normal tool result the model can react to.
SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv("AGENT_SUBPROCESS_TIMEOUT", "120"))


def _timed_out(label: str, captured) -> Dict[str, Any]:
    """Shape a timeout like any other failed run, keeping partial output."""
    def decode(stream):
        if stream is None:
            return ""
        return stream if isinstance(stream, str) else stream.decode("utf-8", "replace")

    return {
        "stdout": decode(getattr(captured, "stdout", "")),
        "stderr": decode(getattr(captured, "stderr", ""))
        + f"\n\n[{label} killed after {SUBPROCESS_TIMEOUT_SECONDS}s. It was "
        "still running, so it produced no result. If it loops over a list "
        "making one API call per item, fetch the data once before the loop "
        "and compare in memory instead.]",
        "exit_code": 124,
        "timed_out": True,
    }


@tool(
    name="run_shell",
    description=(
        "Run a shell command in the container and return stdout, stderr, "
        "and exit code. Defaults to the agent's folder if cwd is not "
        "provided."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory. Defaults to the agent's folder "
                    "under /workspace."
                ),
            },
        },
        "required": ["command"],
    },
)
def run_shell(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    # The captain/worker may pass cwd as the agent's resolved folder; if
    # absent we fall back to the workspace root to match the original.
    command = args.get("command", "")
    workdir = args.get("cwd") or WORKSPACE_DIR
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return _timed_out("run_shell", exc)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


@tool(
    name="run_python",
    description=(
        "Execute a Python code snippet and return stdout, stderr, and "
        "exit code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to run."},
        },
        "required": ["code"],
    },
)
def run_python(args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    code = args.get("code", "")
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=WORKSPACE_DIR,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return _timed_out("run_python", exc)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }
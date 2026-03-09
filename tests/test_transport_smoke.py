"""Best-effort real transport smoke tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fastmcp") is None,
    reason="fastmcp is not installed in this environment.",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _launch(module: str, *, port: str | None = None) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.setdefault("META_ACCESS_TOKEN", "test-token")
    if port is not None:
        env["FASTMCP_PORT"] = port
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=_repo_root(),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _assert_process_boots(proc: subprocess.Popen[str]) -> None:
    time.sleep(0.5)
    if proc.poll() is not None:
        stdout, stderr = proc.communicate(timeout=1)
        raise AssertionError(f"Process exited early with code {proc.returncode}\nstdout={stdout}\nstderr={stderr}")


def _terminate(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def test_stdio_server_boots_with_real_runtime() -> None:
    proc = _launch("meta_ads_mcp.stdio")
    try:
        _assert_process_boots(proc)
    finally:
        _terminate(proc)


def test_http_server_boots_with_real_runtime() -> None:
    proc = _launch("meta_ads_mcp.server", port="8765")
    try:
        _assert_process_boots(proc)
    finally:
        _terminate(proc)

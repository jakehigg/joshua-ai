"""Compose-level smoke test.

Bring the stack up with the stub backend, check that every app answers
`/healthz`, then bring it down. Marked integration, so CI runs it only in
the compose e2e job. Run it locally with `make e2e`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent.parent
# The three app containers all listen on 8000; only channels is published.
CHANNELS_HEALTH = "http://localhost:8080/healthz"


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    # A prepared config and env let the stack boot with no Anthropic token.
    if not (ROOT / "joshua.yaml").exists():
        shutil.copy(ROOT / "joshua.example.yaml", ROOT / "joshua.yaml")
    subprocess.run(["make", "init-env"], cwd=ROOT, check=True)
    env_line = "AGENT_BACKEND=stub\n"
    env_path = ROOT / ".env"
    if env_line not in env_path.read_text():
        env_path.write_text(env_path.read_text() + env_line)
    try:
        _compose("up", "-d", "--build", "--wait")
        yield
    finally:
        _compose("down")


def test_channels_healthz(stack: None) -> None:
    with urllib.request.urlopen(CHANNELS_HEALTH, timeout=10) as response:
        assert response.status == 200
        assert json.load(response) == {"ok": True}

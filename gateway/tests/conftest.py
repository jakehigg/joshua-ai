"""Shared fixtures for the gateway tests.

The upstream is the stdio ``echo_server.py`` fixture. Each test builds a fresh app
from a joshua.yaml written into a temp dir, with fleet tokens set in the
environment. The catalog comes from the ``mcp:`` section of that file.
"""

from __future__ import annotations

import contextlib
import sys
import textwrap
from pathlib import Path

import httpx2
import pytest
from joshua_gateway.main import build_app
from joshua_shared import config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"

# identity -> token, the fleet. `core` is the MCP route caller; `laptop`/`ci` are
# admins; `channels` and `gateway` reach neither the MCP routes nor the admin
# routes on the gateway.
TOKENS = {
    "core": "core-token",
    "channels": "channels-token",
    "gateway": "gateway-token",
    "laptop": "laptop-token",
    "ci": "ci-token",
}


def bearer(identity: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[identity]}"}


def echo_mcp(allow: str = "all", tools_allow: str = "echo") -> str:
    """An ``mcp:`` block with the echo fixture as a single stdio upstream."""
    return textwrap.dedent(f"""\
        echo:
          type: stdio
          command: {sys.executable}
          args:
            - {FIXTURE}
          allow: {allow}
          tools:
            allow:
              - {tools_allow}
        """)


def joshua_yaml(mcp_block: str) -> str:
    """A minimal, valid joshua.yaml carrying ``mcp_block`` under ``mcp:``."""
    return textwrap.dedent("""\
            name: Test
            timezone: America/New_York
            people:
              - id: alex
                name: Alex
              - id: mia
                name: Mia
            mcp:
            """) + textwrap.indent(mcp_block, "  ")


@contextlib.asynccontextmanager
async def gateway_session(
    app, path: str, identity: str, extra_headers: dict[str, str] | None = None
):
    """Open an MCP session to ``path`` through the gateway ``app`` as ``identity``.

    ``extra_headers`` are sent on every request, e.g. ``X-Joshua-Person``. Drives
    the app in-process over an ASGI transport, so the app lifespan must already be
    entered by the caller.
    """
    headers = {**bearer(identity), **(extra_headers or {})}
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers, timeout=30
    )
    try:
        async with streamable_http_client(f"http://testserver{path}", http_client=client) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        await client.aclose()


@pytest.fixture
def config_path(monkeypatch, tmp_path) -> Path:
    """Point the config loader at a temp joshua.yaml and clear its cache."""
    path = tmp_path / "joshua.yaml"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    return path


@pytest.fixture
def gateway(monkeypatch, config_path):
    """Return a builder for a gateway app backed by a temp joshua.yaml.

    Call it with no arguments for the default single ``echo`` upstream, or pass a
    custom ``mcp`` block (the YAML under ``mcp:``).
    """

    def _make(mcp: str | None = None):
        config_path.write_text(joshua_yaml(mcp if mcp is not None else echo_mcp()))
        monkeypatch.setattr(config, "_cache", None)
        for identity, token in TOKENS.items():
            monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
        monkeypatch.delenv("ADMIN_CALLERS", raising=False)
        return build_app()

    return _make

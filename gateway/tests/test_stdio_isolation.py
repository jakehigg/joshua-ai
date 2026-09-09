"""What a stdio child can and cannot see.

The gateway holds every credential in the fleet. A stdio server it starts is
another author's code in the gateway's own container, so it gets its entry's
``env`` and a minimal base, and nothing else. These tests run a real subprocess
and read back its environment, because a test that mocks the spawn proves
nothing about the spawn.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import gateway_session
from joshua_gateway.main import lifespan
from joshua_gateway.proxy import CHILD_BASE_VARS, child_env

ENV_FIXTURE = Path(__file__).parent / "fixtures" / "env_server.py"

# What must never reach a child: the fleet tokens, the database, and the config
# path that names every other entry.
SECRETS = {
    "JOSHUA_TOKEN_CORE": "core-token",
    "JOSHUA_TOKEN_GATEWAY": "gateway-token",
    "JOSHUA_TOKEN_LAPTOP": "laptop-token",
    "DATABASE_URL": "postgresql://joshua:pw@postgres:5432/joshua",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-not-a-real-token",  # gitleaks:allow
}


def env_mcp(extra: str = "") -> str:
    return textwrap.dedent(f"""\
        envdump:
          type: stdio
          command: {sys.executable}
          args:
            - {ENV_FIXTURE}
          allow: all
        """) + textwrap.dedent(extra)


async def _dump_env(app) -> set[str]:
    async with lifespan(app):
        async with gateway_session(app, "/envdump", "core") as session:
            result = await session.call_tool("dump_env", {})
    return set(result.content[0].text.splitlines())


async def test_a_stdio_child_never_sees_a_fleet_token(monkeypatch, gateway):
    """The negative the security docs promise: the agent's tools run beside the
    gateway's credentials and cannot read them."""
    app = gateway(env_mcp())
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    names = await _dump_env(app)
    assert not [n for n in names if n.startswith("JOSHUA_TOKEN_")]
    for name in SECRETS:
        assert name not in names


async def test_a_stdio_child_never_sees_another_entry_credential(monkeypatch, gateway):
    monkeypatch.setenv("OTHER_UPSTREAM_TOKEN", "not-yours")
    app = gateway(
        env_mcp(
            f"""
        other:
          type: stdio
          command: {sys.executable}
          args:
            - {ENV_FIXTURE}
          env:
            OTHER_UPSTREAM_TOKEN: not-yours
          allow: all
        """
        )
    )
    names = await _dump_env(app)
    assert "OTHER_UPSTREAM_TOKEN" not in names


async def test_a_stdio_child_gets_its_own_env_and_a_minimal_base(monkeypatch, gateway):
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "core-token")
    app = gateway(
        textwrap.dedent(f"""\
        envdump:
          type: stdio
          command: {sys.executable}
          args:
            - {ENV_FIXTURE}
          env:
            MY_UPSTREAM_TOKEN: mine
          allow: all
        """)
    )
    names = await _dump_env(app)
    assert "MY_UPSTREAM_TOKEN" in names
    assert "PATH" in names
    assert "JOSHUA_TOKEN_CORE" not in names


# -- child_env, without a subprocess -------------------------------------------


def test_child_env_carries_only_the_base_and_the_entry(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/app")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = child_env({"env": {"WEATHER_KEY": "k"}})
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/app",
        "LANG": "C.UTF-8",
        "WEATHER_KEY": "k",
    }


def test_child_env_puts_the_install_first_on_the_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = child_env({"path_prepend": ["/opt/joshua-mcp/weather/node_modules/.bin"]})
    assert env["PATH"] == "/opt/joshua-mcp/weather/node_modules/.bin:/usr/bin"


def test_child_env_always_has_a_path(monkeypatch):
    for name in CHILD_BASE_VARS:
        monkeypatch.delenv(name, raising=False)
    assert child_env({})["PATH"]


def test_an_entry_may_override_a_base_variable(monkeypatch):
    monkeypatch.setenv("HOME", "/home/app")
    assert child_env({"env": {"HOME": "/opt/joshua-mcp/weather"}})["HOME"] == (
        "/opt/joshua-mcp/weather"
    )


# -- the child's stderr ---------------------------------------------------------


async def test_a_child_stderr_line_reaches_the_gateway_log(gateway, caplog):
    """A server that prints a startup line to stderr must not print it into the
    gateway's own stdout stream, where it would corrupt nothing but confuse
    everything. It is logged, with the entry name."""
    app = gateway(env_mcp())
    with caplog.at_level(logging.INFO, logger="gateway.proxy"):
        await _dump_env(app)
    lines = [r for r in caplog.records if getattr(r, "msg", {}).get("message") == "upstream stderr"]
    assert any(
        r.msg.get("server") == "envdump" and "env_server started" in r.msg.get("line", "")
        for r in lines
    )


@pytest.mark.parametrize("tool", ["dump_cwd"])
async def test_a_plain_stdio_entry_keeps_the_gateway_working_directory(gateway, tool):
    """Only a package entry runs in the store. An entry with a plain ``command``
    keeps today's behaviour, so an existing config does not move."""
    import os

    app = gateway(env_mcp())
    async with lifespan(app):
        async with gateway_session(app, "/envdump", "core") as session:
            result = await session.call_tool(tool, {})
    assert result.content[0].text == os.getcwd()

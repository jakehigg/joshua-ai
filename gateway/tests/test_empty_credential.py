"""An entry whose credential expanded to nothing is turned off, not retried.

``${VAR:-}`` in a container without the variable expands to the empty string.
Starting the server anyway is worse than not starting it: an empty ``env`` value
reaches the upstream as no credential at all, and a header of ``Bearer `` is an
illegal HTTP header, which kills the connection and retries forever. The entry
goes to ``disabled`` instead, with one log line, and comes up on the reload after
the value is set.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import httpx2
from conftest import bearer, echo_mcp
from joshua_gateway.catalog import blank_credential
from joshua_gateway.main import lifespan

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"


def stdio_with_token(var: str = "UPSTREAM_TOKEN") -> str:
    return textwrap.dedent(f"""\
        weather:
          type: stdio
          command: {sys.executable}
          args:
            - {FIXTURE}
          env:
            ECHO_IDENTITY: "${{{var}:-}}"
          allow: all
        """)


HTTP_WITH_BEARER = textwrap.dedent("""\
    hello:
      type: http
      url: http://127.0.0.1:9/mcp
      headers:
        Authorization: "Bearer ${HELLO_ADDON_TOKEN:-}"
      allow: all
    """)


async def _get(app, path: str, identity: str):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=bearer(identity)
    ) as client:
        return await client.get(path)


async def _post(app, path: str, identity: str, body: dict):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=bearer(identity), timeout=30
    ) as client:
        return await client.post(path, json=body)


# -- the rule itself -----------------------------------------------------------


def test_an_empty_env_value_is_a_blank_credential():
    assert blank_credential({"env": {"TOKEN": ""}}) == "env TOKEN"
    assert blank_credential({"env": {"TOKEN": "   "}}) == "env TOKEN"
    assert blank_credential({"env": {"TOKEN": "abc"}}) is None


def test_a_header_that_is_only_a_scheme_is_a_blank_credential():
    assert blank_credential({"headers": {"Authorization": "Bearer "}}) == "header Authorization"
    assert blank_credential({"headers": {"Authorization": "Basic"}}) == "header Authorization"
    assert blank_credential({"headers": {"X-Api-Key": ""}}) == "header X-Api-Key"
    assert blank_credential({"headers": {"Authorization": "Bearer abc"}}) is None


def test_an_entry_with_no_credential_at_all_is_not_blank():
    assert blank_credential({"type": "stdio", "command": "mcp-weather"}) is None


# -- what the gateway does with it ---------------------------------------------


async def test_a_stdio_entry_with_an_empty_credential_never_starts(monkeypatch, gateway, caplog):
    monkeypatch.delenv("UPSTREAM_TOKEN", raising=False)
    app = gateway(stdio_with_token())
    with caplog.at_level(logging.WARNING, logger="gateway.upstream"):
        async with lifespan(app):
            up = app.state.upstreams["weather"]
            assert up.status == "disabled"
            assert up.connected is False
            assert up.error is None, "a missing credential is not a connect failure"
    disabled = [r for r in caplog.records if r.msg.get("message", "").startswith("upstream disab")]
    assert len(disabled) == 1
    assert disabled[0].msg["detail"] == "env ECHO_IDENTITY"


async def test_an_http_entry_with_an_empty_bearer_does_not_crash_loop(monkeypatch, gateway):
    """The illegal `Bearer ` header never reaches httpx, so the upstream shows as
    disabled instead of retrying a LocalProtocolError forever."""
    monkeypatch.delenv("HELLO_ADDON_TOKEN", raising=False)
    app = gateway(HTTP_WITH_BEARER)
    async with lifespan(app):
        assert app.state.upstreams["hello"].status == "disabled"
        assert app.state.upstreams["hello"].error is None


async def test_readyz_counts_a_disabled_entry_apart_from_an_errored_one(monkeypatch, gateway):
    monkeypatch.delenv("UPSTREAM_TOKEN", raising=False)
    app = gateway(stdio_with_token() + echo_mcp())
    async with lifespan(app):
        body = (await _get(app, "/readyz", "core")).json()
    assert body == {
        "ok": False,
        "connected": 1,
        "errored": 0,
        "installing": 0,
        "disabled": 1,
        "total": 2,
    }


async def test_the_inventory_says_why_an_entry_is_disabled(monkeypatch, gateway):
    monkeypatch.delenv("UPSTREAM_TOKEN", raising=False)
    app = gateway(stdio_with_token())
    async with lifespan(app):
        body = (await _get(app, "/admin/inventory", "laptop")).json()
    entry = body["servers"]["weather"]
    assert entry["status"] == "disabled"
    assert entry["disabled_reason"] == "env ECHO_IDENTITY"


async def test_setting_the_credential_and_reloading_starts_the_entry(
    monkeypatch, gateway, config_path
):
    monkeypatch.delenv("UPSTREAM_TOKEN", raising=False)
    app = gateway(stdio_with_token())
    async with lifespan(app):
        assert app.state.upstreams["weather"].status == "disabled"
        monkeypatch.setenv("UPSTREAM_TOKEN", "now-i-have-one")
        result = (await _post(app, "/admin/reload", "laptop", {})).json()
        assert result["failed"] == {}
        assert app.state.upstreams["weather"].status == "connected"


async def test_emptying_a_credential_and_reloading_disables_a_running_entry(
    monkeypatch, gateway, config_path
):
    monkeypatch.setenv("UPSTREAM_TOKEN", "have-one")
    app = gateway(stdio_with_token())
    async with lifespan(app):
        assert app.state.upstreams["weather"].status == "connected"
        monkeypatch.delenv("UPSTREAM_TOKEN")
        result = (await _post(app, "/admin/reload", "laptop", {})).json()
        assert result["failed"] == {}, "a disabled entry is not a failed reload"
        assert result["reloaded"]["weather"] == {"disabled": "env ECHO_IDENTITY"}
        up = app.state.upstreams["weather"]
        await up.wait_connected(0.5)
        assert up.status == "disabled"

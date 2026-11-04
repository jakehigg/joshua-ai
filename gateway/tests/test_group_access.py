"""Group access: a scoped server whose ``allow`` names a group chat.

Core sends ``X-Joshua-Group`` for a group chat. The gateway trusts it only from
``core``, admits the group to a server whose ``allow`` names it, and routes it to
the group's instance on an identity server. The echo fixture's ``whoami`` tool
returns the ``ECHO_IDENTITY`` env var, so a call proves which instance served it.
"""

from __future__ import annotations

import sys

import httpx2
import pytest
from conftest import FIXTURE, TOKENS, bearer, gateway_session
from joshua_gateway.main import build_app, lifespan
from joshua_gateway.person import Attribution, PersonError, resolve
from joshua_shared import config

ROSTER = (
    "name: Test\n"
    "timezone: America/New_York\n"
    "people:\n"
    "  - id: alex\n    name: Alex\n"
    "  - id: mia\n    name: Mia\n"
    "groups:\n"
    "  - id: team\n    channel: telegram\n    chat_id: '-100200'\n"
    "    members:\n      - '101'\n"
    "  - id: other\n    channel: telegram\n    chat_id: '-100300'\n"
    "    members:\n      - '101'\n"
)


def _entry(name: str, allow: list[str], identities: dict[str, str] | None = None) -> str:
    allow_block = "".join(f"      - {item}\n" for item in allow)
    text = (
        f"  {name}:\n"
        "    type: stdio\n"
        f"    command: {sys.executable}\n"
        "    args:\n"
        f"      - {FIXTURE}\n"
        "    allow:\n"
        f"{allow_block}"
        "    tools:\n"
        "      allow:\n"
        "        - echo\n"
        "        - whoami\n"
    )
    if identities:
        text += "    identities:\n" + "".join(
            f"      {key}:\n        env:\n          ECHO_IDENTITY: {value}\n"
            for key, value in identities.items()
        )
    return text


def catalog() -> str:
    """``shared`` is one instance for alex and the team chat. ``tickets`` runs one
    instance for alex and one for the team chat."""
    return (
        ROSTER
        + "mcp:\n"
        + _entry("shared", ["alex", "team"])
        + _entry("tickets", ["alex", "team"], {"alex": "alex-value", "team": "team-value"})
    )


def build(monkeypatch, config_path):
    config_path.write_text(catalog())
    monkeypatch.setattr(config, "_cache", None)
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    return build_app()


def headers(person: str = "unknown", group: str | None = None) -> dict[str, str]:
    out = {"X-Joshua-Person": person, "X-Joshua-Conversation": "c1"}
    if group is not None:
        out["X-Joshua-Group"] = group
    return out


async def _get(app, path: str, extra: dict[str, str]):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers={**bearer("core"), **extra})


# -- resolve() unit -------------------------------------------------------


def test_resolve_trusts_a_group_from_core():
    attr = resolve("core", "unknown", "c1", {"alex"}, "member", "team", {"team"})
    assert attr == Attribution(person="unknown", conversation="c1", role="member", group="team")


def test_resolve_ignores_a_group_from_another_caller():
    attr = resolve("laptop", "unknown", "c1", {"alex"}, "member", "team", {"team"})
    assert attr.group is None


def test_resolve_rejects_an_unknown_group():
    with pytest.raises(PersonError, match="unknown group: nobody"):
        resolve("core", "unknown", "c1", {"alex"}, "member", "nobody", {"team"})


# -- a shared instance ----------------------------------------------------


async def test_an_allowed_group_uses_a_scoped_server(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        async with gateway_session(app, "/shared", "core", headers(group="team")) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {"echo", "whoami"}
            result = await session.call_tool("echo", {"message": "hi"})
            assert not result.is_error


async def test_another_group_is_denied(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        resp = await _get(app, "/shared", headers(group="other"))
    assert resp.status_code == 403


async def test_no_group_and_no_allowed_person_is_denied(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        unknown = await _get(app, "/shared", headers())
        mia = await _get(app, "/shared", headers("mia"))
    assert unknown.status_code == 403
    assert mia.status_code == 403


async def test_an_unknown_group_header_is_400(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        resp = await _get(app, "/shared", headers(group="nobody"))
    assert resp.status_code == 400


# -- an identity server ---------------------------------------------------


async def _whoami(app, extra: dict[str, str]) -> str:
    async with gateway_session(app, "/tickets", "core", extra) as session:
        return (await session.call_tool("whoami", {})).content[0].text


async def test_a_group_uses_its_own_identity(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        assert await _whoami(app, headers(group="team")) == "team-value"
        assert await _whoami(app, headers("alex")) == "alex-value"


async def test_a_person_identity_comes_before_the_group(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        assert await _whoami(app, headers("alex", "team")) == "alex-value"
        assert await _whoami(app, headers("mia", "team")) == "team-value"


async def test_another_group_is_denied_on_an_identity_server(monkeypatch, config_path):
    app = build(monkeypatch, config_path)
    async with lifespan(app):
        resp = await _get(app, "/tickets", headers(group="other"))
    assert resp.status_code == 403

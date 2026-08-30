"""Per-person policy: the person header trust rule, the per-server person gate,
session person/conversation binding, and the person on the call log.

Two catalog entries drive it: ``a`` (``allow: all``) and ``b`` (``allow: [alex]``),
both backed by the echo fixture.
"""

from __future__ import annotations

import sys

import httpx2
import pytest
from conftest import FIXTURE, TOKENS, bearer, gateway_session
from joshua_gateway import person
from joshua_gateway.main import lifespan
from joshua_gateway.observability import CALL_LOG, SESSIONS
from joshua_gateway.person import Attribution, PersonError, resolve
from starlette.testclient import TestClient


def _echo_entry(name: str, allow_yaml: str) -> str:
    """One echo-fixture entry with a given ``allow`` block (kept as block YAML,
    since the loader rejects flow style)."""
    return (
        f"{name}:\n"
        f"  type: stdio\n"
        f"  command: {sys.executable}\n"
        f"  args:\n"
        f"    - {FIXTURE}\n"
        f"{allow_yaml}"
        f"  tools:\n"
        f"    allow:\n"
        f"      - echo\n"
    )


def two_servers() -> str:
    """``a`` open to everyone, ``b`` scoped to alex; both run the echo fixture."""
    return _echo_entry("a", "  allow: all\n") + _echo_entry("b", "  allow:\n    - alex\n")


def person_header(person: str | None = None, conversation: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if person is not None:
        headers["X-Joshua-Person"] = person
    if conversation is not None:
        headers["X-Joshua-Conversation"] = conversation
    return headers


async def _get(app, path: str, identity: str, extra: dict[str, str] | None = None):
    """GET a gateway path in-process; the app lifespan must already be entered."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers={**bearer(identity), **(extra or {})})


# -- resolve() unit -------------------------------------------------------


def test_resolve_ignores_header_from_untrusted_identity():
    attr = resolve("channels", "alex", "c1", {"alex"})
    assert attr == Attribution(person=None, conversation=None)


def test_resolve_trusts_core_and_validates():
    attr = resolve("core", "alex", "c1", {"alex"})
    assert attr == Attribution(person="alex", conversation="c1")


def test_resolve_allows_literal_unknown():
    assert resolve("core", "unknown", None, {"alex"}).person == "unknown"


def test_resolve_rejects_unknown_person():
    with pytest.raises(PersonError):
        resolve("core", "ghost", None, {"alex"})


def test_resolve_rejects_malformed_person():
    with pytest.raises(PersonError):
        resolve("core", "Bad Name!", None, {"alex"})


# -- tools/list per person (acceptance) -----------------------------------


async def test_mia_sees_a_but_not_b(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        async with gateway_session(app, "/a", "core", person_header("mia")) as session:
            assert [t.name for t in (await session.list_tools()).tools] == ["echo"]
        resp = await _get(app, "/b", "core", person_header("mia"))
    assert resp.status_code == 403


async def test_alex_sees_both(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        async with gateway_session(app, "/a", "core", person_header("alex")) as session:
            assert [t.name for t in (await session.list_tools()).tools] == ["echo"]
        async with gateway_session(app, "/b", "core", person_header("alex")) as session:
            assert [t.name for t in (await session.list_tools()).tools] == ["echo"]


async def test_no_person_sees_only_a(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        async with gateway_session(app, "/a", "core") as session:
            assert [t.name for t in (await session.list_tools()).tools] == ["echo"]
        resp = await _get(app, "/b", "core")
    assert resp.status_code == 403


# -- only core is trusted (acceptance) ------------------------------------


def test_laptop_person_header_is_ignored(gateway):
    with TestClient(gateway(two_servers())) as client:
        resp = client.get("/b", headers={**bearer("laptop"), **person_header("alex")})
    # The header is ignored, so the request has no person; b denies no person.
    assert resp.status_code == 403


def test_no_identity_but_core_is_trusted():
    """Only core may say which person a request belongs to.

    resolve() ignores the person header from every other fleet identity, so the
    request has no person and gets what an unknown person gets. The header
    decides who reads and writes a person's files, and nothing widens the rule.
    """
    for identity in sorted(set(TOKENS) - {"core"}):
        assert resolve(identity, "alex", "c1", {"alex"}) == Attribution(
            person=None, conversation=None
        )
    assert person.trusted_identities() == frozenset({"core"})


def test_laptop_is_refused_on_an_mcp_route(gateway):
    """The MCP routes carry a person's tools and files. Only core reaches them."""
    with TestClient(gateway(two_servers())) as client:
        resp = client.get("/a", headers=bearer("laptop"))
    assert resp.status_code == 403


# -- bad person header (acceptance) ---------------------------------------


def test_malformed_person_is_400(gateway):
    with TestClient(gateway(two_servers())) as client:
        resp = client.get("/a", headers={**bearer("core"), **person_header("Bad Name!")})
    assert resp.status_code == 400


def test_unknown_person_id_is_400(gateway):
    with TestClient(gateway(two_servers())) as client:
        resp = client.get("/a", headers={**bearer("core"), **person_header("ghost")})
    assert resp.status_code == 400


# -- session person / conversation change (acceptance) --------------------


async def test_session_person_change_is_409(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        # A live session on "a" is bound to alex; the mismatched request below is
        # rejected before it reaches the upstream, so no manager round trip runs.
        SESSIONS.check("a", "sess-person", "alex", "c1")
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/a",
                headers={
                    **bearer("core"),
                    "Mcp-Session-Id": "sess-person",
                    "X-Joshua-Person": "mia",
                },
                content=b"{}",
            )
    assert resp.status_code == 409
    assert b"person_changed" in resp.content


async def test_session_conversation_change_is_409(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        SESSIONS.check("a", "sess-conv", "alex", "c1")
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/a",
                headers={
                    **bearer("core"),
                    "Mcp-Session-Id": "sess-conv",
                    "X-Joshua-Person": "alex",
                    "X-Joshua-Conversation": "c2",
                },
                content=b"{}",
            )
    assert resp.status_code == 409
    assert b"conversation_changed" in resp.content


# -- call log and inventory -----------------------------------------------


def test_denied_request_is_logged_with_person(gateway):
    with TestClient(gateway(two_servers())) as client:
        resp = client.get("/b", headers={**bearer("core"), **person_header("mia")})
        assert resp.status_code == 403
    denied = [c for c in CALL_LOG.recent() if c["status"] == "denied" and c["server"] == "b"]
    assert denied, "a person-denied request must be logged"
    assert denied[0]["person"] == "mia"
    assert denied[0]["error"] == "person_not_allowed"


async def test_inventory_reports_allow_and_live_persons(gateway):
    app = gateway(two_servers())
    async with lifespan(app):
        SESSIONS.check("b", "live-1", "alex", "c1")  # one live session on b
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", headers=bearer("laptop")
        ) as client:
            inventory = (await client.get("/admin/inventory")).json()
    servers = inventory["servers"]
    assert servers["a"]["allow"] == "all"
    assert servers["b"]["allow"] == ["alex"]
    assert servers["a"]["persons"] == []
    assert servers["b"]["persons"] == ["alex"]

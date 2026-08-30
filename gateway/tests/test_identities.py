"""Per-person upstream identities.

A server with ``identities`` runs one upstream instance per person, each started
with that person's own connect overrides. A request is routed to the instance for
its person; a request with no person is 403 ``person_required``. The echo fixture's
``whoami`` tool returns the ``ECHO_IDENTITY`` env var, so a call proves which
instance served it.
"""

from __future__ import annotations

import sys

import httpx2
import pytest
from conftest import FIXTURE, TOKENS, bearer, gateway_session
from joshua_gateway.catalog import build_catalog, instance_specs
from joshua_gateway.main import build_app, lifespan, reconcile_catalog
from joshua_shared import config
from joshua_shared.config import ConfigError

PEOPLE = ("alex", "mia", "sam")


def spotify_yaml(identities: dict[str, str], allow: list[str]) -> str:
    """A stdio ``spotify`` entry backed by the echo fixture, with a per-person
    ``ECHO_IDENTITY`` env for each identity and the given ``allow`` list."""
    people = "".join(f"  - id: {p}\n    name: {p.title()}\n" for p in PEOPLE)
    allow_block = "".join(f"      - {p}\n" for p in allow)
    id_block = "".join(
        f"      {person}:\n        env:\n          ECHO_IDENTITY: {value}\n"
        for person, value in identities.items()
    )
    return (
        "name: Test\n"
        "timezone: America/New_York\n"
        "people:\n"
        f"{people}"
        "mcp:\n"
        "  spotify:\n"
        "    type: stdio\n"
        f"    command: {sys.executable}\n"
        "    args:\n"
        f"      - {FIXTURE}\n"
        "    allow:\n"
        f"{allow_block}"
        "    tools:\n"
        "      allow:\n"
        "        - whoami\n"
        "    identities:\n"
        f"{id_block}"
    )


def both() -> str:
    return spotify_yaml({"alex": "alex-value", "mia": "mia-value"}, ["alex", "mia"])


def build(monkeypatch, config_path, text: str):
    """Write ``text`` to the config path, mint fleet tokens, and build an app."""
    config_path.write_text(text)
    monkeypatch.setattr(config, "_cache", None)
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    return build_app()


async def _get(app, path: str, identity: str, extra: dict[str, str] | None = None):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers={**bearer(identity), **(extra or {})})


def person(pid: str) -> dict[str, str]:
    return {"X-Joshua-Person": pid}


# -- instance fan-out unit -------------------------------------------------


def test_instance_specs_merges_per_person_env():
    spec = build_catalog(config.parse(both(), {}, source="t.yaml"))["spotify"]
    insts = instance_specs(spec)
    assert set(insts) == {"alex", "mia"}
    assert insts["alex"].connect_cfg["env"]["ECHO_IDENTITY"] == "alex-value"
    assert insts["mia"].connect_cfg["env"]["ECHO_IDENTITY"] == "mia-value"


def test_instance_specs_no_identities_is_single_shared():
    text = (
        "name: Test\ntimezone: America/New_York\npeople:\n"
        "  - id: alex\n    name: Alex\n"
        "mcp:\n  spotify:\n    type: stdio\n"
        f"    command: {sys.executable}\n    args:\n      - {FIXTURE}\n"
        "    allow: all\n"
    )
    spec = build_catalog(config.parse(text, {}, source="t.yaml"))["spotify"]
    assert set(instance_specs(spec)) == {None}


# -- acceptance: each person gets their own credential ---------------------


async def test_each_person_calls_with_their_own_identity(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        async with gateway_session(app, "/spotify", "core", person("alex")) as session:
            assert (await session.call_tool("whoami", {})).content[0].text == "alex-value"
        async with gateway_session(app, "/spotify", "core", person("mia")) as session:
            assert (await session.call_tool("whoami", {})).content[0].text == "mia-value"


async def test_third_person_is_denied(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        resp = await _get(app, "/spotify", "core", person("sam"))
    assert resp.status_code == 403


async def test_no_person_is_person_required(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        resp = await _get(app, "/spotify", "core")
    assert resp.status_code == 403
    assert b"person_required" in resp.content


# -- acceptance: reload removes one instance, leaves the other -------------


async def test_reload_removes_one_person_keeps_the_other(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        async with gateway_session(app, "/spotify", "core", person("alex")) as session:
            assert (await session.call_tool("whoami", {})).content[0].text == "alex-value"

            config_path.write_text(spotify_yaml({"alex": "alex-value"}, ["alex"]))
            result = await reconcile_catalog(app)
            assert result["reloaded"]["spotify"]["instances"]["mia"] == {"removed": True}
            assert "spotify:mia" not in app.state.upstreams

            # Alex's live session is untouched: the same session still works.
            assert (await session.call_tool("whoami", {})).content[0].text == "alex-value"

        # Mia now has no instance, so she is denied.
        resp = await _get(app, "/spotify", "core", person("mia"))
    assert resp.status_code == 403


# -- acceptance: config validation ----------------------------------------


def test_unknown_identity_person_fails_validation():
    text = spotify_yaml({"alex": "j"}, ["alex"]).replace(
        "      alex:\n        env:\n          ECHO_IDENTITY: j\n",
        "      ghost:\n        env:\n          ECHO_IDENTITY: g\n",
    )
    with pytest.raises(ConfigError) as exc:
        config.parse(text, {}, source="test.yaml")
    assert "spotify" in str(exc.value)
    assert "ghost" in str(exc.value)


def test_allowed_person_without_identity_fails_validation():
    with pytest.raises(ConfigError) as exc:
        config.parse(spotify_yaml({"alex": "j"}, ["alex", "mia"]), {}, source="test.yaml")
    assert "mia is allowed but has no identity" in str(exc.value)


def test_identity_env_rejected_on_http_server():
    text = (
        "name: Test\ntimezone: America/New_York\npeople:\n"
        "  - id: alex\n    name: Alex\n"
        "mcp:\n  spotify:\n    type: http\n    url: https://spotify.example/mcp\n"
        "    allow:\n      - alex\n    identities:\n"
        "      alex:\n        env:\n          TOKEN: x\n"
    )
    with pytest.raises(ConfigError) as exc:
        config.parse(text, {}, source="test.yaml")
    assert "stdio" in str(exc.value)


# -- inventory and readiness ----------------------------------------------


async def test_inventory_lists_instances(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        inv = (await _get(app, "/admin/inventory", "laptop")).json()
    spot = inv["servers"]["spotify"]
    assert spot["label"] == "spotify"
    assert spot["identities"] == ["alex", "mia"]
    assert sorted(i["person"] for i in spot["instances"]) == ["alex", "mia"]
    assert all(i["status"] == "connected" for i in spot["instances"])


async def test_readyz_counts_each_instance(monkeypatch, config_path):
    app = build(monkeypatch, config_path, both())
    async with lifespan(app):
        body = (await _get(app, "/readyz", "core")).json()
    assert body == {"ok": True, "connected": 2, "errored": 0, "total": 2}

"""A ``package`` entry end to end: install, start, reload, reinstall, fail alone.

The store holds a shell shim that starts the ``echo_server`` fixture, so the
gateway resolves a real command out of a real store directory and speaks MCP to
a real subprocess. No install reaches the network: an entry whose store already
matches its spec never installs, and the tests that need an install replace
``mcp_store.install``.
"""

from __future__ import annotations

import json
import logging
import sys
import textwrap
from pathlib import Path

import httpx2
import pytest
from conftest import bearer, gateway_session
from joshua_gateway import main as gateway_main
from joshua_gateway import mcp_store
from joshua_gateway.main import lifespan

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"
SHIM = f'#!/bin/sh\nexec {sys.executable} {FIXTURE} "$@"\n'


@pytest.fixture
def quick_boot(monkeypatch):
    """Do not hold the boot open for an entry that will never connect.

    The supervisor retries in the background either way, so a test that wants an
    upstream in its error state need not wait out the production timeout.
    """
    monkeypatch.setattr(gateway_main, "BOOT_CONNECT_TIMEOUT_S", 1.0)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Point the store at a temp directory and return a builder for one entry."""
    root = tmp_path / "store"
    monkeypatch.setenv(mcp_store.STORE_ENV_VAR, str(root))

    def install_shim(name: str, package: str = "npm:echo-mcp@1.0.0", **extra) -> dict:
        """Write what a finished install of ``package`` looks like, and return its record."""
        entry = root / name
        binary = entry / "bin" / "echo-mcp"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(SHIM)
        binary.chmod(0o755)
        record = {
            "name": name,
            "package": package,
            "kind": "npm",
            "bare_name": "echo-mcp",
            "registry": None,
            "allow_scripts": False,
            "sha256": None,
            "resolved": package.rsplit("@", 1)[-1],
            "installed_at": "2026-09-08T00:00:00+00:00",
            "bin_dirs": ["bin"],
            **extra,
        }
        (entry / mcp_store.RECORD_NAME).write_text(json.dumps(record))
        return record

    install_shim.root = root
    return install_shim


def package_mcp(name: str = "weather", package: str = "npm:echo-mcp@1.0.0") -> str:
    return textwrap.dedent(f"""\
        {name}:
          type: stdio
          package: {package}
          allow: all
          tools:
            allow:
              - echo
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


# -- the happy path ------------------------------------------------------------


async def test_a_package_entry_starts_from_the_store(gateway, store, caplog):
    """A store that already holds the pinned spec starts the server with no
    install, so a restart with no network still works. It must not even say it
    is installing: an operator reading /readyz would see a state that is not
    happening."""
    store("weather")
    app = gateway(package_mcp())
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with lifespan(app):
            async with gateway_session(app, "/weather", "core") as session:
                names = [t.name for t in (await session.list_tools()).tools]
                assert names == ["echo"]
                result = await session.call_tool("echo", {"message": "hi"})
            assert app.state.upstreams["weather"].installing is False
    assert result.content[0].text == "echo: hi"
    said = [r for r in caplog.records if r.msg.get("message") == "installing mcp package"]
    assert said == []


async def test_the_inventory_shows_the_package_and_what_is_installed(gateway, store):
    store("weather")
    app = gateway(package_mcp())
    async with lifespan(app):
        body = (await _get(app, "/admin/inventory", "laptop")).json()
    entry = body["servers"]["weather"]
    assert entry["status"] == "connected"
    assert entry["package"] == "npm:echo-mcp@1.0.0"
    assert entry["resolved"] == "1.0.0"
    assert entry["installed_at"] == "2026-09-08T00:00:00+00:00"


async def test_an_entry_without_a_package_shows_no_install_fields(gateway):
    app = gateway()
    async with lifespan(app):
        body = (await _get(app, "/admin/inventory", "laptop")).json()
    assert "package" not in body["servers"]["echo"]


async def test_a_package_child_runs_in_its_own_store_directory(gateway, store):
    """The working directory is the entry's own store directory, so a server that
    writes beside itself cannot reach another entry or the data volume."""
    store("weather")
    entry = mcp_store.entry_dir("weather")
    app = gateway(package_mcp())
    async with lifespan(app):
        up = app.state.upstreams["weather"]
        cfg = up._resolved_cfg()
    assert cfg["cwd"] == str(entry)
    assert cfg["command"] == str(entry / "bin" / "echo-mcp")
    assert cfg["path_prepend"] == [str(entry / "bin")]


# -- installing ----------------------------------------------------------------


async def test_a_version_bump_reinstalls_that_entry_and_leaves_the_others(
    gateway, store, monkeypatch, config_path
):
    store("weather")
    store("news")
    app = gateway(package_mcp("weather") + package_mcp("news"))

    installed: list[str] = []

    def fake_install(request, **kwargs):
        installed.append(f"{request.name}:{request.spec.raw}")
        return store(request.name, package=request.spec.raw)

    monkeypatch.setattr(mcp_store, "install", fake_install)

    async with lifespan(app):
        news_before = app.state.upstreams["news"].mgr
        assert installed == [], "a matching store must not install at boot"

        config_path.write_text(
            config_path.read_text().replace("npm:echo-mcp@1.0.0", "npm:echo-mcp@2.0.0", 1)
        )
        result = (await _post(app, "/admin/reload", "laptop", {})).json()

        assert installed == ["weather:npm:echo-mcp@2.0.0"]
        assert result["reloaded"]["news"].get("unchanged") is True
        assert app.state.upstreams["news"].mgr is news_before, "news kept its session"
        assert app.state.upstreams["weather"].status == "connected"


async def test_admin_install_forces_a_reinstall_and_reconnects(gateway, store, monkeypatch):
    store("weather")
    app = gateway(package_mcp())
    calls: list[str] = []

    def fake_install(request, **kwargs):
        calls.append(request.name)
        return store(request.name)

    monkeypatch.setattr(mcp_store, "install", fake_install)

    async with lifespan(app):
        response = await _post(
            app, "/admin/mcp/install", "laptop", {"server": "weather", "reinstall": True}
        )
        body = response.json()
        assert response.status_code == 200
        assert calls == ["weather"]
        assert body["resolved"] == "1.0.0"
        assert body["reconnected"] == ["weather"]
        # The entry is serving again on the new install.
        async with gateway_session(app, "/weather", "core") as session:
            assert (await session.call_tool("echo", {"message": "x"})).content[0].text == "echo: x"


async def test_admin_install_without_reinstall_uses_the_store(gateway, store, monkeypatch):
    store("weather")
    app = gateway(package_mcp())
    monkeypatch.setattr(
        mcp_store, "install", lambda *a, **k: pytest.fail("the store already matches")
    )
    async with lifespan(app):
        response = await _post(app, "/admin/mcp/install", "laptop", {"server": "weather"})
    assert response.status_code == 200


async def test_admin_install_names_the_package_servers_when_asked_for_another(gateway, store):
    store("weather")
    app = gateway(
        package_mcp()
        + "\n"
        + textwrap.dedent(f"""\
        plain:
          type: stdio
          command: {sys.executable}
          args:
            - {FIXTURE}
          allow: all
        """)
    )
    async with lifespan(app):
        for target in ({"server": "plain"}, {"server": "absent"}, {}):
            response = await _post(app, "/admin/mcp/install", "laptop", target)
            assert response.status_code == 404
            assert response.json()["known"] == ["weather"]


async def test_admin_install_needs_an_admin_caller(gateway, store):
    store("weather")
    app = gateway(package_mcp())
    async with lifespan(app):
        assert (
            await _post(app, "/admin/mcp/install", "core", {"server": "weather"})
        ).status_code == 403


async def test_a_failed_install_reports_a_reason_and_no_token(
    gateway, store, monkeypatch, quick_boot
):
    """One entry that cannot install must not take the others down, and its
    reason must be readable without being a stack trace or a credential."""
    store("news")
    app = gateway(package_mcp("weather") + package_mcp("news"))

    def fake_install(request, **kwargs):
        if request.name == "weather":
            raise mcp_store.InstallError("npm failed: 404 Not Found - npm:echo-mcp@1.0.0")
        return store(request.name)

    monkeypatch.setattr(mcp_store, "install", fake_install)

    async with lifespan(app):
        ready = (await _get(app, "/readyz", "core")).json()
        assert ready == {
            "ok": False,
            "connected": 1,
            "errored": 1,
            "installing": 0,
            "disabled": 0,
            "total": 2,
        }
        # The entry that installed is still serving.
        async with gateway_session(app, "/news", "core") as session:
            assert (await session.call_tool("echo", {"message": "y"})).content[0].text == "echo: y"
        error = app.state.upstreams["weather"].error
    assert "404 Not Found" in error
    assert "\n" not in error
    assert "Traceback" not in error


async def test_the_install_failure_is_visible_in_the_inventory(
    gateway, store, monkeypatch, quick_boot
):
    app = gateway(package_mcp("weather"))
    monkeypatch.setattr(
        mcp_store,
        "install",
        lambda *a, **k: (_ for _ in ()).throw(
            mcp_store.InstallError("npm failed: no such package")
        ),
    )
    async with lifespan(app):
        body = (await _get(app, "/admin/inventory", "laptop")).json()
    entry = body["servers"]["weather"]
    assert entry["status"] == "error"
    assert "no such package" in entry["error"]
    assert entry["resolved"] is None

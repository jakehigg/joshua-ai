import httpx
import pytest
from adapter_fakes import FakeAdapter
from joshua_channels.__main__ import main
from joshua_channels.app import build_app
from joshua_channels.deliver import ChannelsContext
from joshua_channels.registry import AdapterHealth, AdapterRegistry
from joshua_shared import config

MINIMAL = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def test_startup_aborts_on_bad_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setenv("JOSHUA_CONFIG", str(tmp_path / "absent.yaml"))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def _client(*adapters: FakeAdapter) -> httpx.AsyncClient:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    settings = config.parse(MINIMAL, env={}, source="<test>")
    app = build_app(ChannelsContext(settings=settings, registry=registry))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


async def test_readyz_reports_failing_adapter() -> None:
    failing = FakeAdapter(
        "telegram",
        health=AdapterHealth(ok=False, reason="Conflict: terminated by other getUpdates request"),
    )
    async with _client(failing) as client:
        body = (await client.get("/readyz")).json()
    assert body["ok"] is False
    assert body["checks"]["telegram"]["ok"] is False
    assert "Conflict" in body["checks"]["telegram"]["reason"]


async def test_readyz_reports_healthy_adapter() -> None:
    async with _client(FakeAdapter("telegram")) as client:
        body = (await client.get("/readyz")).json()
    assert body["ok"] is True
    assert body["checks"] == {"telegram": {"ok": True}}


async def test_readyz_with_no_adapters_is_ok_and_empty() -> None:
    async with _client() as client:
        body = (await client.get("/readyz")).json()
    assert body["ok"] is True
    assert body["checks"] == {}


# -- the status code is the answer ---------------------------------------------


async def test_readyz_answers_503_when_an_adapter_is_failing() -> None:
    """A probe reads the code. A body under a 200 is a probe that cannot fail."""
    failing = FakeAdapter("telegram", health=AdapterHealth(ok=False, reason="Conflict"))
    async with _client(failing) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ok"] is False


async def test_readyz_answers_200_when_every_adapter_is_well() -> None:
    async with _client(FakeAdapter("telegram")) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ok"] is True

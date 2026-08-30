"""Integration test for the boot sequence: lifespan, schema, seed, probes."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from joshua_core.main import build_app
from joshua_core.store.db import Database
from joshua_core.store.repo import Repo
from joshua_shared import config

pytestmark = pytest.mark.integration

CONFIG_YAML = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
"""


def test_boot_applies_schema_and_seeds(monkeypatch, tmp_path) -> None:
    path = tmp_path / "joshua.yaml"
    path.write_text(CONFIG_YAML)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    # The lifespan now wires the deliverer; it needs the channels URL.
    monkeypatch.setenv("CHANNELS_URL", "http://channels:8000")
    # The lifespan bootstraps the data volume; point it at a tmp dir.
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path / "data"))

    app = build_app()
    with TestClient(app) as client:
        # The lifespan connected the DB, applied the schema, seeded people, and
        # bootstrapped the data volume.
        assert client.get("/healthz").json() == {"ok": True}
        ready = client.get("/readyz").json()
        assert ready == {
            "ok": True,
            "checks": {"db": True, "layout": True, "embed": True},
        }

        # Seeding ran during startup; verify from an independent connection.
        people = asyncio.run(_list_people())
        assert {p.id for p in people} == {"alex"}


async def _list_people():
    database = Database(os.environ["DATABASE_URL"])
    await database.connect()
    try:
        return await Repo(database).list_people()
    finally:
        await database.close()

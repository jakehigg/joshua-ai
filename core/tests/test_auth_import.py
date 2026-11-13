"""The one-time import of the viewer passwords, and the four rules that hold it.

A fake repo stands in for the database. Its ``transaction`` keeps a copy of the
secrets and the marker and puts it back on an error, the way Postgres rolls a
transaction back, so the all-or-nothing rule is testable with no database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from joshua_core import auth_api, auth_import
from joshua_core.auth_import import import_once
from joshua_core.store.models import Person
from joshua_shared import config as config_module

CONFIG_YAML = """
name: Test House
timezone: UTC
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
viewer:
  enabled: true
  users:
    alex: "{alex}"
    mia: "{mia}"
"""

# A bcrypt hash of "alex-password", at the lowest cost. It is stored as it is.
ALEX_HASH = auth_api.hash_secret("alex-password")


def _settings(alex: str = "", mia: str = "") -> Any:
    return config_module.parse(CONFIG_YAML.format(alex=alex, mia=mia))


class FakeRepo:
    """The roster, the secrets, and the marker, in memory."""

    def __init__(self, people: list[str] | None = None) -> None:
        self.people = {p: Person(id=p, display_name=p.title()) for p in (people or ["alex", "mia"])}
        self.secrets: dict[str, str] = {}
        self.marker: dict[str, Any] | None = None
        # The person id whose write raises, for the failure test.
        self.fail_on: str | None = None

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        secrets, marker = dict(self.secrets), self.marker
        try:
            yield "conn"
        except Exception:
            self.secrets, self.marker = secrets, marker
            raise

    async def get_person(self, person_id: str) -> Person | None:
        return self.people.get(person_id)

    async def get_auth_secret(self, person_id: str, *, conn: Any = None) -> str | None:
        return self.secrets.get(person_id)

    async def set_auth_secret(self, person_id: str, secret_hash: str, *, conn: Any = None) -> None:
        if person_id == self.fail_on:
            raise RuntimeError("the database went away")
        self.secrets[person_id] = secret_hash

    async def get_auth_import_marker(self, *, conn: Any = None) -> dict[str, Any] | None:
        return self.marker

    async def set_auth_import_marker(self, rows_written: int, *, conn: Any = None) -> None:
        self.marker = {"id": "viewer_passwords", "rows_written": rows_written}


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lowest bcrypt cost. The import hashes, and no test checks timing."""
    monkeypatch.setattr(auth_api, "BCRYPT_ROUNDS", 4)


@pytest.fixture(autouse=True)
def no_viewer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIEWER_PASSWORDS", raising=False)
    monkeypatch.delenv("VIEWER_PW_ALEX", raising=False)
    monkeypatch.delenv("VIEWER_PW_MIA", raising=False)


# --- the three sources -----------------------------------------------------


async def test_import_reads_the_config_entry() -> None:
    repo = FakeRepo()

    summary = await import_once(repo, _settings(alex="alex-password"))

    assert summary == {"ran": True, "imported": 1, "skipped": 0, "unreadable": 1}
    assert auth_api.verify_secret("alex-password", repo.secrets["alex"])
    # A person with no reference anywhere gets no row.
    assert "mia" not in repo.secrets


async def test_import_reads_viewer_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIEWER_PASSWORDS", f"alex={ALEX_HASH},mia=mia-password")
    repo = FakeRepo()

    summary = await import_once(repo, _settings())

    assert summary["imported"] == 2
    # A $2 reference is already a hash: it is stored as it is, not hashed again.
    assert repo.secrets["alex"] == ALEX_HASH
    assert repo.secrets["mia"] != "mia-password"
    assert auth_api.verify_secret("mia-password", repo.secrets["mia"])


async def test_import_reads_viewer_pw_of_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIEWER_PW_MIA", "mia-password")
    repo = FakeRepo()

    await import_once(repo, _settings())

    assert auth_api.verify_secret("mia-password", repo.secrets["mia"])


async def test_import_skips_a_person_off_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIEWER_PASSWORDS", "alex=alex-password,mia=mia-password")
    repo = FakeRepo(people=["alex"])

    summary = await import_once(repo, _settings())

    assert summary == {"ran": True, "imported": 1, "skipped": 1, "unreadable": 0}
    assert set(repo.secrets) == {"alex"}


# --- rule a: the marker, never "the table is empty" ------------------------


async def test_the_marker_is_set_after_a_run() -> None:
    repo = FakeRepo()
    await import_once(repo, _settings(alex="alex-password"))
    assert repo.marker == {"id": "viewer_passwords", "rows_written": 1}


async def test_a_second_start_is_a_no_op_even_with_no_secrets() -> None:
    """The marker alone decides. An empty auth_secret never re-runs the import."""
    repo = FakeRepo()
    await import_once(repo, _settings(alex="alex-password"))
    # Every secret is dropped, as a restore or a reset would drop them.
    repo.secrets.clear()

    summary = await import_once(repo, _settings(alex="alex-password"))

    assert summary == {"ran": False, "imported": 0, "skipped": 0, "unreadable": 0}
    assert repo.secrets == {}


# --- rule b: never overwrite an existing row -------------------------------


async def test_an_existing_row_is_never_overwritten() -> None:
    repo = FakeRepo()
    repo.secrets["alex"] = "$2b$04$the-row-already-there"

    summary = await import_once(repo, _settings(alex="alex-password", mia="mia-password"))

    assert summary == {"ran": True, "imported": 1, "skipped": 1, "unreadable": 0}
    assert repo.secrets["alex"] == "$2b$04$the-row-already-there"
    assert auth_api.verify_secret("mia-password", repo.secrets["mia"])


# --- rule c and d: one transaction, and core still starts ------------------


async def test_a_failing_import_leaves_no_rows_and_no_marker() -> None:
    repo = FakeRepo()
    repo.fail_on = "mia"

    summary = await import_once(repo, _settings(alex="alex-password", mia="mia-password"))

    # Nothing raised: core starts.
    assert summary == {"ran": False, "imported": 0, "skipped": 0, "unreadable": 0}
    # Neither half-imported rows nor a marker: the next start tries again.
    assert repo.secrets == {}
    assert repo.marker is None


async def test_the_next_start_after_a_failure_imports_everything() -> None:
    repo = FakeRepo()
    repo.fail_on = "mia"
    await import_once(repo, _settings(alex="alex-password", mia="mia-password"))

    repo.fail_on = None
    summary = await import_once(repo, _settings(alex="alex-password", mia="mia-password"))

    assert summary["imported"] == 2
    assert repo.marker == {"id": "viewer_passwords", "rows_written": 2}


async def test_a_failing_marker_write_leaves_nothing_behind() -> None:
    repo = FakeRepo()

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the database went away")

    repo.set_auth_import_marker = fail  # type: ignore[assignment]

    assert await import_once(repo, _settings(alex="alex-password")) == {
        "ran": False,
        "imported": 0,
        "skipped": 0,
        "unreadable": 0,
    }
    assert repo.secrets == {}
    assert repo.marker is None


# --- the log ---------------------------------------------------------------


async def test_the_run_logs_one_line_per_person_and_one_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[dict[str, Any]] = []
    monkeypatch.setattr(
        auth_import.logger, "info", lambda payload: lines.append(payload), raising=False
    )
    repo = FakeRepo()
    repo.secrets["mia"] = "$2b$04$the-row-already-there"

    await import_once(repo, _settings(alex="alex-password"))

    assert [line["message"] for line in lines] == [
        "auth import imported",
        "auth import skipped",
        "auth import done",
    ]
    # No line carries a secret or a hash.
    assert not [line for line in lines if "alex-password" in str(line) or "$2" in str(line)]

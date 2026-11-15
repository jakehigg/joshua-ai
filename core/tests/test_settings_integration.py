"""``repo.settings`` against a real Postgres: a row survives, a secret is
unreadable in the table, and a prefix reads back a group."""

from __future__ import annotations

import base64
import os

import pytest
from joshua_core.store.settings import SETTINGS_KEY_ENV, SettingsKeyError

pytestmark = pytest.mark.integration

PUBLIC = "push.vapid_public_key"
PRIVATE = "push.vapid_private_key"
SECRET_VALUE = "a private key nobody may read back"


@pytest.fixture
def settings_key(monkeypatch) -> None:
    monkeypatch.setenv(SETTINGS_KEY_ENV, base64.b64encode(os.urandom(32)).decode("ascii"))


async def test_set_and_get_a_plain_setting(repo, settings_key) -> None:
    assert await repo.settings.get(PUBLIC) is None
    written = await repo.settings.set(PUBLIC, "a-public-key", updated_by="mira")
    assert written.value == "a-public-key"
    stored = await repo.settings.get(PUBLIC)
    assert stored is not None
    assert (stored.value, stored.is_secret, stored.updated_by) == ("a-public-key", False, "mira")
    assert stored.updated_at is not None
    # A second write replaces the one row.
    await repo.settings.set(PUBLIC, "another-public-key", updated_by="mira")
    again = await repo.settings.get(PUBLIC)
    assert again is not None and again.value == "another-public-key"


async def test_a_secret_is_a_ciphertext_in_the_table(repo, db, settings_key) -> None:
    await repo.settings.set(PRIVATE, SECRET_VALUE, is_secret=True, updated_by="mira")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value, is_secret FROM settings WHERE key = %s", (PRIVATE,))
        row = await cur.fetchone()
    assert row is not None and row["is_secret"] is True
    assert SECRET_VALUE not in row["value"]
    # Server code reads the plain value back, and a route reads only is_set.
    stored = await repo.settings.get(PRIVATE)
    assert stored is not None and stored.value == SECRET_VALUE
    hidden = await repo.settings.get(PRIVATE, decrypt=False)
    assert hidden is not None and (hidden.value, hidden.is_set) == ("", True)


async def test_get_many_reads_the_group_and_delete_drops_a_row(repo, settings_key) -> None:
    await repo.settings.set(PUBLIC, "a-public-key")
    await repo.settings.set(PRIVATE, SECRET_VALUE, is_secret=True)
    rows = await repo.settings.get_many("push", decrypt=False)
    assert [row.key for row in rows] == [PRIVATE, PUBLIC]
    assert await repo.settings.get_many("nothing") == []
    assert await repo.settings.delete(PUBLIC) is True
    assert await repo.settings.delete(PUBLIC) is False
    assert [row.key for row in await repo.settings.get_many("push", decrypt=False)] == [PRIVATE]


async def test_a_secret_write_with_no_key_writes_nothing(repo, monkeypatch) -> None:
    monkeypatch.delenv(SETTINGS_KEY_ENV, raising=False)
    with pytest.raises(SettingsKeyError) as exc:
        await repo.settings.set(PRIVATE, SECRET_VALUE, is_secret=True)
    assert SETTINGS_KEY_ENV in str(exc.value)
    assert await repo.settings.get(PRIVATE) is None
    # A plain setting still goes in with no key in the environment.
    await repo.settings.set(PUBLIC, "a-public-key")
    stored = await repo.settings.get(PUBLIC)
    assert stored is not None and stored.value == "a-public-key"

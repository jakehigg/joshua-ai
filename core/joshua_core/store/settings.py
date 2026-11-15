"""The settings of the instance: one row per setting, a secret one encrypted.

A setting lives in the database, not in the environment, so the owner writes it
on the Instance page of the viewer and a restart keeps it. The ``settings``
table holds one row per setting: the key, the value, whether the value is a
secret, when it changed, and who changed it.

A key is namespaced by a dotted prefix. The part before the first dot is the
group, such as ``push``, and the rest names the setting, as in
``push.vapid_public_key``. ``SETTING_GROUPS`` declares every group, its label,
and the fields of it, so a later feature adds a group here and the admin page
renders it with no change of its own. A route writes a declared key only.

A secret value is encrypted at rest with AES-GCM: a random 12-byte nonce, the
ciphertext beside it, and the key of the setting as the associated data, so a
ciphertext cannot be moved from one setting to another. The stored text is
``v1:<base64 of nonce and ciphertext>``, so a secret is unreadable in the
database. ``get`` decrypts it for server code; no route puts a secret value in
an answer.

The encryption key comes from the environment, ``JOSHUA_SETTINGS_KEY``, a
base64 32-byte key. Generate one with::

    openssl rand -base64 32

With the variable unset, a read of a non-secret setting and a write of one both
work, and a write or a read of a secret raises ``SettingsKeyError``, which
names the variable. There is no fallback to plaintext, and no key is minted at
startup: a key that changes makes every stored secret unreadable.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from joshua_shared.log import get_logger

from joshua_core.store.db import Database

logger = get_logger("store.settings")

# The environment variable that holds the key every secret setting is
# encrypted with. It is never stored and never answered.
SETTINGS_KEY_ENV = "JOSHUA_SETTINGS_KEY"
# A base64 32-byte key, and the nonce AES-GCM takes.
KEY_BYTES = 32
NONCE_BYTES = 12
# The marker of a stored secret. A value without it was not encrypted here.
VERSION_PREFIX = "v1:"

# The longest key and value a route accepts.
KEY_MAX = 128
VALUE_MAX = 4096

MISSING_KEY = (
    f"{SETTINGS_KEY_ENV} is not set: a secret setting cannot be read or written "
    "without it. Generate one with `openssl rand -base64 32`, put it in .env, "
    "and restart."
)
BAD_KEY = f"{SETTINGS_KEY_ENV} is not a base64 {KEY_BYTES}-byte key."
BAD_SECRET = (
    f"a stored secret does not read with this {SETTINGS_KEY_ENV}: it was written with another key."
)


class SettingsKeyError(RuntimeError):
    """``JOSHUA_SETTINGS_KEY`` is missing, is not a base64 32-byte key, or is
    not the key a stored secret was written with."""


@dataclass(frozen=True)
class SettingField:
    """One setting a group declares: its key, what the page calls it, and
    whether its value is a secret."""

    key: str
    label: str
    secret: bool = False
    hint: str = ""


@dataclass(frozen=True)
class SettingGroup:
    """One section of the Instance page: a dotted prefix and its fields."""

    prefix: str
    label: str
    fields: tuple[SettingField, ...]


@dataclass(frozen=True)
class Setting:
    """One row of ``settings``.

    ``value`` is the plain value: the stored text of a non-secret, and the
    decrypted text of a secret that was read with ``get``. It is ``""`` for a
    secret that was read without decrypting, so ``is_set`` says whether the
    secret is there.
    """

    key: str
    value: str
    is_secret: bool
    is_set: bool
    updated_at: datetime | None = None
    updated_by: str | None = None


# Every group of settings, in the order the Instance page shows them. Push
# notifications are the first consumer: the switch that turns them on, the
# VAPID pair of the instance, the public key the browser subscribes with and
# the private key that signs a push, and the subject of each signature. Push
# is off in every deployment until the owner writes these four here: no
# environment variable turns it on (docs/notifications.md). A later feature
# adds its group here.
SETTING_GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        prefix="push",
        label="Notifications",
        fields=(
            SettingField(
                key="push.enabled",
                label="Push notifications on",
                hint='Type "true" to turn them on, once the three keys below are set.',
            ),
            SettingField(
                key="push.vapid_public_key",
                label="VAPID public key",
                hint="The browser subscribes with this key.",
            ),
            SettingField(
                key="push.vapid_private_key",
                label="VAPID private key",
                secret=True,
                hint="Signs each push. It never leaves the server.",
            ),
            SettingField(
                key="push.vapid_subject",
                label="VAPID subject",
                hint="Who a push service writes to about this instance: "
                "https://joshua.example, or mailto:you@example.com.",
            ),
        ),
    ),
)


def find_group(prefix: str) -> SettingGroup | None:
    """The group of that dotted prefix, or None when nothing declares it."""
    for group in SETTING_GROUPS:
        if group.prefix == prefix:
            return group
    return None


def find_field(key: str) -> SettingField | None:
    """The declared field of that key, or None for a key no group declares."""
    for group in SETTING_GROUPS:
        for field in group.fields:
            if field.key == key:
                return field
    return None


def load_key() -> bytes:
    """The 32 raw bytes of ``JOSHUA_SETTINGS_KEY``.

    Raises ``SettingsKeyError`` with ``MISSING_KEY`` when the variable is
    absent or empty, and with ``BAD_KEY`` when it is not base64 or is not 32
    bytes long. Nothing falls back to plaintext, and no key is minted here.
    """
    raw = os.environ.get(SETTINGS_KEY_ENV, "").strip()
    if not raw:
        raise SettingsKeyError(MISSING_KEY)
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SettingsKeyError(BAD_KEY) from exc
    if len(key) != KEY_BYTES:
        raise SettingsKeyError(BAD_KEY)
    return key


def encrypt_value(key: str, plain: str) -> str:
    """The stored text of a secret value: ``v1:<base64 of nonce and ciphertext>``.

    The key of the setting is the associated data, so a ciphertext only reads
    back under the key it was written for.
    """
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(load_key()).encrypt(nonce, plain.encode("utf-8"), key.encode("utf-8"))
    return VERSION_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_value(key: str, stored: str) -> str:
    """The plain value of a stored secret.

    Raises ``SettingsKeyError`` when ``JOSHUA_SETTINGS_KEY`` is missing or
    malformed, when the stored text was not written by ``encrypt_value``, and
    when it was written with another key.
    """
    cipher = AESGCM(load_key())
    if not stored.startswith(VERSION_PREFIX):
        raise SettingsKeyError(BAD_SECRET)
    try:
        blob = base64.b64decode(stored[len(VERSION_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SettingsKeyError(BAD_SECRET) from exc
    nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        plain = cipher.decrypt(nonce, sealed, key.encode("utf-8"))
    except InvalidTag as exc:
        raise SettingsKeyError(BAD_SECRET) from exc
    return plain.decode("utf-8")


class SettingsStore:
    """The ``settings`` table: get one, get many by prefix, set one, delete one.

    ``Repo`` holds one of these as ``repo.settings``. Every secret value is
    encrypted on the way in and decrypted on the way out, so a caller passes
    and gets the plain value and the database holds only the ciphertext.
    """

    def __init__(self, db: Database):
        self._db = db

    @property
    def _pool(self):  # type: ignore[no-untyped-def]
        assert self._db.pool is not None, "Database not connected"
        return self._db.pool

    async def get(self, key: str, *, decrypt: bool = True) -> Setting | None:
        """One setting, or None when no row holds that key.

        A secret is decrypted for the server code that asked, which needs
        ``JOSHUA_SETTINGS_KEY``. With ``decrypt`` false the value stays out of
        the answer, and ``is_set`` says whether a secret is stored: that is
        what a route reads, so a list works with no key in the environment.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM settings WHERE key = %s", (key,))
            row = await cur.fetchone()
        return self._setting(row, decrypt=decrypt) if row else None

    async def get_many(self, prefix: str, *, decrypt: bool = True) -> list[Setting]:
        """Every setting whose key is the prefix or starts with ``<prefix>.``.

        An empty prefix gives every setting. The rows come back by key.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM settings WHERE key = %s OR key LIKE %s ORDER BY key",
                (prefix, f"{prefix}.%") if prefix else ("", "%"),
            )
            rows = await cur.fetchall()
        return [self._setting(row, decrypt=decrypt) for row in rows]

    async def set(
        self,
        key: str,
        value: str,
        *,
        is_secret: bool = False,
        updated_by: str | None = None,
    ) -> Setting:
        """Write one setting, and answer what is stored.

        A secret is encrypted first, so the plain value never reaches the
        database, and the answer carries no value for it. A write of a secret
        with no ``JOSHUA_SETTINGS_KEY`` raises ``SettingsKeyError``, before any
        SQL runs.
        """
        stored = encrypt_value(key, value) if is_secret else value
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO settings (key, value, is_secret, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    is_secret = EXCLUDED.is_secret,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                RETURNING *
                """,
                (key, stored, is_secret, updated_by),
            )
            row = await cur.fetchone()
        logger.info(
            {
                "message": "setting written",
                "key": key,
                "secret": is_secret,
                "by": updated_by,
            }
        )
        assert row is not None  # RETURNING of an upsert always gives the row
        return self._setting(row, decrypt=False)

    async def delete(self, key: str) -> bool:
        """Drop one setting. False when no row held that key."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM settings WHERE key = %s", (key,))
        return cur.rowcount > 0

    def _setting(self, row: dict[str, Any], *, decrypt: bool) -> Setting:
        """One row as a ``Setting``. A secret value is decrypted when asked
        for, and left out otherwise."""
        stored = row["value"] or ""
        is_secret = bool(row["is_secret"])
        value = stored
        if is_secret:
            value = decrypt_value(row["key"], stored) if decrypt and stored else ""
        return Setting(
            key=row["key"],
            value=value,
            is_secret=is_secret,
            is_set=bool(stored),
            updated_at=row.get("updated_at"),
            updated_by=row.get("updated_by"),
        )

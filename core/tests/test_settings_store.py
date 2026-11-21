"""Tests for the settings store: the encryption, the missing key, the routes.

The encryption tests are the store's own (``store/settings.py``): a secret
reads back under its key and under no other, and every error names
``JOSHUA_SETTINGS_KEY``. The route tests use a fake store, so they say what
``/admin/settings`` answers without a database: the guard, the check of each
push setting, that no answer carries a secret value, and that a secret write
with no key is a 503 that names the variable.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from joshua_core.settings_api import SETTINGS_PATH, build_settings_router
from joshua_core.store.settings import (
    BOOL_KIND,
    SECRET_KIND,
    SETTINGS_KEY_ENV,
    TEXT_KIND,
    VERSION_PREFIX,
    Setting,
    SettingsKeyError,
    decrypt_value,
    encrypt_value,
    find_field,
    find_group,
)
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
VIEWER = "viewer-token"
CHANNELS = "channels-token"

ENABLED = "push.enabled"
PUBLIC = "push.vapid_public_key"
PRIVATE = "push.vapid_private_key"
SUBJECT = "push.vapid_subject"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _a_vapid_pair() -> tuple[str, str]:
    """One VAPID pair, as the owner pastes it: base64url, public first."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url(public), _b64url(key.private_numbers().private_value.to_bytes(32, "big"))


# One pair for this module: a write of a key is checked against its shape.
PUBLIC_VALUE, SECRET_VALUE = _a_vapid_pair()
SUBJECT_VALUE = "mailto:owner@example.test"


def _key() -> str:
    """One invented key of the right shape, made for this test alone."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture
def settings_key(monkeypatch) -> str:
    key = _key()
    monkeypatch.setenv(SETTINGS_KEY_ENV, key)
    return key


@pytest.fixture
def no_settings_key(monkeypatch) -> None:
    monkeypatch.delenv(SETTINGS_KEY_ENV, raising=False)


class FakeSettingsStore:
    """``repo.settings`` in a dict, with the store's own encryption.

    It keeps what the table keeps, so a secret is a ciphertext here too and a
    route that answers a value would show it.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def _row(self, row: dict[str, object], *, decrypt: bool) -> Setting:
        stored = str(row["value"])
        is_secret = bool(row["is_secret"])
        value = stored
        if is_secret:
            value = decrypt_value(str(row["key"]), stored) if decrypt and stored else ""
        return Setting(
            key=str(row["key"]),
            value=value,
            is_secret=is_secret,
            is_set=bool(stored),
            updated_at=row["updated_at"],  # type: ignore[arg-type]
            updated_by=row["updated_by"],  # type: ignore[arg-type]
        )

    async def get(self, key: str, *, decrypt: bool = True) -> Setting | None:
        row = self.rows.get(key)
        return self._row(row, decrypt=decrypt) if row else None

    async def get_many(self, prefix: str, *, decrypt: bool = True) -> list[Setting]:
        keys = [key for key in self.rows if key == prefix or key.startswith(f"{prefix}.")]
        return [self._row(self.rows[key], decrypt=decrypt) for key in sorted(keys)]

    async def set(
        self,
        key: str,
        value: str,
        *,
        is_secret: bool = False,
        updated_by: str | None = None,
    ) -> Setting:
        stored = encrypt_value(key, value) if is_secret else value
        row = {
            "key": key,
            "value": stored,
            "is_secret": is_secret,
            "updated_at": datetime(2026, 11, 15, 10, tzinfo=UTC),
            "updated_by": updated_by,
        }
        self.rows[key] = row
        return self._row(row, decrypt=False)

    async def delete(self, key: str) -> bool:
        return self.rows.pop(key, None) is not None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    store = FakeSettingsStore()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=SimpleNamespace(settings=store))
    app.include_router(build_settings_router())
    tc = TestClient(app)
    tc.store = store  # type: ignore[attr-defined]
    return tc


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER}"}


def _settings(answer: dict) -> dict[str, dict]:
    """Every field of every group of an answer, by key."""
    return {field["key"]: field for group in answer["groups"] for field in group["settings"]}


# --- the encryption ---------------------------------------------------------


def test_encrypt_hides_the_value(settings_key) -> None:
    stored = encrypt_value(PRIVATE, SECRET_VALUE)
    assert stored.startswith(VERSION_PREFIX)
    assert SECRET_VALUE not in stored


def test_decrypt_reads_back_what_was_written(settings_key) -> None:
    assert decrypt_value(PRIVATE, encrypt_value(PRIVATE, SECRET_VALUE)) == SECRET_VALUE


def test_each_write_is_a_new_nonce(settings_key) -> None:
    first = encrypt_value(PRIVATE, SECRET_VALUE)
    second = encrypt_value(PRIVATE, SECRET_VALUE)
    assert first != second
    assert decrypt_value(PRIVATE, second) == SECRET_VALUE


def test_a_ciphertext_does_not_move_to_another_setting(settings_key) -> None:
    # The key of the setting is the associated data.
    stored = encrypt_value(PRIVATE, SECRET_VALUE)
    with pytest.raises(SettingsKeyError) as exc:
        decrypt_value(PUBLIC, stored)
    assert SETTINGS_KEY_ENV in str(exc.value)


def test_another_key_does_not_read_a_secret(monkeypatch, settings_key) -> None:
    stored = encrypt_value(PRIVATE, SECRET_VALUE)
    monkeypatch.setenv(SETTINGS_KEY_ENV, _key())
    with pytest.raises(SettingsKeyError) as exc:
        decrypt_value(PRIVATE, stored)
    assert "another key" in str(exc.value)


def test_missing_key_names_the_variable(no_settings_key) -> None:
    with pytest.raises(SettingsKeyError) as exc:
        encrypt_value(PRIVATE, SECRET_VALUE)
    assert SETTINGS_KEY_ENV in str(exc.value)
    with pytest.raises(SettingsKeyError) as exc:
        decrypt_value(PRIVATE, f"{VERSION_PREFIX}whatever")
    assert SETTINGS_KEY_ENV in str(exc.value)


@pytest.mark.parametrize("bad", ["not base64 at all", base64.b64encode(b"short").decode()])
def test_a_key_of_the_wrong_shape_is_refused(monkeypatch, bad) -> None:
    monkeypatch.setenv(SETTINGS_KEY_ENV, bad)
    with pytest.raises(SettingsKeyError) as exc:
        encrypt_value(PRIVATE, SECRET_VALUE)
    assert SETTINGS_KEY_ENV in str(exc.value)


# --- the groups -------------------------------------------------------------


def test_a_key_is_namespaced_by_its_prefix() -> None:
    group = find_group("push")
    assert group is not None
    # The three keys first, and the switch that turns push on last.
    assert [field.key for field in group.fields] == [PUBLIC, PRIVATE, SUBJECT, ENABLED]
    assert all(field.key.startswith(f"{group.prefix}.") for field in group.fields)
    assert find_group("nothing") is None
    assert find_field("push.nothing") is None


def test_each_field_says_what_kind_of_value_it_holds() -> None:
    kinds = {key: find_field(key) for key in (PUBLIC, PRIVATE, SUBJECT, ENABLED)}
    assert all(field is not None for field in kinds.values())
    assert kinds[PUBLIC].kind == TEXT_KIND  # type: ignore[union-attr]
    assert kinds[SUBJECT].kind == TEXT_KIND  # type: ignore[union-attr]
    assert kinds[ENABLED].kind == BOOL_KIND  # type: ignore[union-attr]
    assert kinds[PRIVATE].kind == SECRET_KIND  # type: ignore[union-attr]
    # The secret is the one field whose value never comes back.
    assert [key for key, field in kinds.items() if field.is_secret] == [PRIVATE]  # type: ignore[union-attr]


# --- the guard --------------------------------------------------------------


def test_settings_require_a_bearer(client) -> None:
    assert client.get(SETTINGS_PATH).status_code == 401
    assert client.post(SETTINGS_PATH, json={"key": PUBLIC, "value": "x"}).status_code == 401


def test_settings_forbid_a_caller_outside_the_allowlist(client) -> None:
    # channels authenticates but is in neither ADMIN_CALLERS nor AUTH_CALLERS.
    headers = {"Authorization": f"Bearer {CHANNELS}"}
    denied = client.get(SETTINGS_PATH, headers=headers)
    assert denied.status_code == 403
    assert "settings" in denied.json()["error"]
    assert (
        client.post(SETTINGS_PATH, headers=headers, json={"key": PUBLIC, "value": "x"}).status_code
        == 403
    )


def test_the_viewer_and_an_admin_caller_both_pass(client) -> None:
    assert client.get(SETTINGS_PATH, headers=_viewer()).status_code == 200
    assert (
        client.get(SETTINGS_PATH, headers={"Authorization": f"Bearer {LAPTOP}"}).status_code == 200
    )


# --- the routes -------------------------------------------------------------


def test_get_lists_every_declared_field(client, no_settings_key) -> None:
    # With no key in the environment: the list is not a secret read.
    answer = client.get(SETTINGS_PATH, headers=_viewer()).json()
    assert [group["prefix"] for group in answer["groups"]] == ["push", "workspaces", "admin"]
    fields = _settings(answer)
    assert fields[PUBLIC] == {
        "key": PUBLIC,
        "label": "VAPID public key",
        "hint": "The browser subscribes with this key.",
        "kind": TEXT_KIND,
        "is_secret": False,
        "is_set": False,
        "value": "",
        "updated_at": None,
        "updated_by": None,
    }
    assert fields[PRIVATE]["is_secret"] is True
    assert fields[PRIVATE]["kind"] == SECRET_KIND
    assert fields[ENABLED]["kind"] == BOOL_KIND


def test_get_of_an_unknown_prefix_is_404(client) -> None:
    denied = client.get(f"{SETTINGS_PATH}?prefix=nothing", headers=_viewer())
    assert denied.status_code == 404
    assert "nothing" in denied.json()["error"]


def test_post_writes_a_plain_setting(client, settings_key) -> None:
    resp = client.post(
        SETTINGS_PATH,
        headers=_viewer(),
        json={"key": PUBLIC, "value": PUBLIC_VALUE, "person": "mira"},
    )
    assert resp.status_code == 200
    assert resp.json()["setting"]["value"] == PUBLIC_VALUE
    assert client.store.rows[PUBLIC]["value"] == PUBLIC_VALUE
    assert client.store.rows[PUBLIC]["updated_by"] == "mira"
    # And the list reads it back, by prefix.
    fields = _settings(client.get(f"{SETTINGS_PATH}?prefix=push", headers=_viewer()).json())
    assert fields[PUBLIC]["value"] == PUBLIC_VALUE
    assert fields[PUBLIC]["updated_by"] == "mira"


def test_post_writes_a_secret_and_no_answer_carries_it(client, settings_key) -> None:
    resp = client.post(
        SETTINGS_PATH, headers=_viewer(), json={"key": PRIVATE, "value": SECRET_VALUE}
    )
    assert resp.status_code == 200
    assert resp.json()["setting"] == {
        "key": PRIVATE,
        "label": "VAPID private key",
        "hint": "Signs each push. It never leaves the server.",
        "kind": SECRET_KIND,
        "is_secret": True,
        "is_set": True,
        "value": None,
        "updated_at": "2026-11-15T10:00:00+00:00",
        "updated_by": None,
    }
    # The stored text is the ciphertext, and the list says only that it is set.
    assert SECRET_VALUE not in str(client.store.rows[PRIVATE]["value"])
    listed = _settings(client.get(SETTINGS_PATH, headers=_viewer()).json())[PRIVATE]
    assert listed["is_set"] is True
    assert listed["value"] is None
    assert SECRET_VALUE not in client.get(SETTINGS_PATH, headers=_viewer()).text
    # Server code still reads it back.
    assert client.store.rows[PRIVATE]["is_secret"] is True


def test_a_secret_write_with_no_key_is_refused(client, no_settings_key) -> None:
    resp = client.post(
        SETTINGS_PATH, headers=_viewer(), json={"key": PRIVATE, "value": SECRET_VALUE}
    )
    assert resp.status_code == 503
    assert SETTINGS_KEY_ENV in resp.json()["error"]
    assert PRIVATE not in client.store.rows
    # The rest of the settings still work.
    assert (
        client.post(
            SETTINGS_PATH, headers=_viewer(), json={"key": PUBLIC, "value": PUBLIC_VALUE}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "body",
    [
        {"key": "push.nothing", "value": "x"},
        {"key": "", "value": "x"},
        {"key": PUBLIC, "value": 7},
        {"key": PUBLIC, "value": "x", "colour": "blue"},
        {"key": PRIVATE, "value": ""},
    ],
)
def test_post_refuses_a_bad_body(client, settings_key, body) -> None:
    resp = client.post(SETTINGS_PATH, headers=_viewer(), json=body)
    assert resp.status_code == 400
    assert resp.json()["error"]


# --- the check of each push setting ------------------------------------------


def _write(client, key: str, value: str):  # type: ignore[no-untyped-def]
    return client.post(SETTINGS_PATH, headers=_viewer(), json={"key": key, "value": value})


@pytest.mark.parametrize(
    "subject",
    ["owner@example.test", "http://joshua.example", "joshua.example", "", "  "],
)
def test_a_subject_that_is_not_a_mailto_or_an_https_url_is_refused(
    client, settings_key, subject
) -> None:
    """``py_vapid`` raises ``VapidException`` on such a subject in the middle of
    a send, so the write is where the owner hears about it."""
    resp = _write(client, SUBJECT, subject)
    assert resp.status_code == 400
    line = resp.json()["error"]
    assert "mailto:" in line and "https://" in line
    assert SUBJECT not in client.store.rows


@pytest.mark.parametrize("subject", [SUBJECT_VALUE, "https://joshua.example"])
def test_a_mailto_or_an_https_subject_is_written(client, settings_key, subject) -> None:
    assert _write(client, SUBJECT, subject).status_code == 200
    assert client.store.rows[SUBJECT]["value"] == subject


@pytest.mark.parametrize(
    "value",
    [
        "not base64url at all!",
        _b64url(b"\x04" + b"a" * 32),  # 33 bytes: too short for a P-256 point
        _b64url(bytes([0x02]) + b"a" * 64),  # 65 bytes, but not an uncompressed point
    ],
)
def test_a_public_key_of_the_wrong_shape_is_refused(client, settings_key, value) -> None:
    resp = _write(client, PUBLIC, value)
    assert resp.status_code == 400
    line = resp.json()["error"]
    assert "65 bytes" in line and "0x04" in line
    assert PUBLIC not in client.store.rows


@pytest.mark.parametrize("value", ["not base64url at all!", _b64url(b"a" * 31), PUBLIC_VALUE])
def test_a_private_key_of_the_wrong_shape_is_refused(client, settings_key, value) -> None:
    """The public key pasted into the private field is the likely mistake, and
    it is 65 bytes where 32 belong."""
    resp = _write(client, PRIVATE, value)
    assert resp.status_code == 400
    assert "32 bytes" in resp.json()["error"]
    assert value not in resp.json()["error"]
    assert PRIVATE not in client.store.rows


def test_the_pair_of_the_command_is_written(client, settings_key) -> None:
    assert _write(client, PUBLIC, PUBLIC_VALUE).status_code == 200
    assert _write(client, PRIVATE, SECRET_VALUE).status_code == 200
    assert _write(client, ENABLED, "true").status_code == 200
    assert client.store.rows[ENABLED]["value"] == "true"
    assert _write(client, ENABLED, "false").status_code == 200
    assert client.store.rows[ENABLED]["value"] == "false"

"""``/admin/settings``: the settings of the instance, as the owner edits them.

Core owns the database, so a setting lives here and nowhere else
(``store/settings.py``). The viewer proxies these two routes to the owner
under ``/api/v1/admin/settings/``, as the identity ``viewer``.

- ``GET /admin/settings?prefix=<prefix>`` — the groups of that dotted prefix,
  each with its fields and what is stored for each. With no ``prefix`` it is
  every group. A prefix no group declares is 404.
- ``POST /admin/settings`` — ``{key, value, person?}``: write one setting.
  ``SETTING_GROUPS`` says what kind the key is, so a caller can never turn a
  secret into a plain value. A key no group declares is 400.

**A value that the feature cannot use is refused here, not later.** ``CHECKS``
holds one check per key, and a refusal is a 400 whose line says what the field
takes: the VAPID subject must be a ``mailto:`` or an ``https://`` address,
which is what ``py_vapid`` raises ``VapidException`` for in the middle of a
send, each VAPID key must be base64url of the right length, the default
servers of a new workspace must be MCP server names, and the switch that skips
the approval of a pull request must be a yes, a no, or empty. A refusal names
no value: the private key is a secret.

**No answer carries a secret value.** A field says its ``kind``, ``is_secret``
and ``is_set``, and ``value`` is null for a secret, whether it is stored or
not. The owner replaces a secret by writing a new one; nothing reads the old
one back.

A write of a secret needs ``JOSHUA_SETTINGS_KEY`` (see
``store/settings.py``). With the variable unset, that one write is 503 and its
``error`` names the variable; a read of the list and a write of a plain
setting still work.

The caller check is ``authorize_settings``: an admin caller, or one of
``AUTH_CALLERS`` (default ``viewer``). It is checked on both routes with no
exception.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.config import MCP_NAME_PATTERN
from joshua_shared.log import get_logger

from joshua_core.admin import authorize_settings
from joshua_core.developer import FALSE_WORDS, SKIP_APPROVAL_KEY, TRUE_WORDS
from joshua_core.push import PRIVATE_KEY, PUBLIC_KEY, SUBJECT_KEY
from joshua_core.store.settings import (
    KEY_MAX,
    SETTING_GROUPS,
    VALUE_MAX,
    Setting,
    SettingField,
    SettingGroup,
    SettingsKeyError,
    find_field,
    find_group,
)
from joshua_core.update import DEFAULT_REPO, REPO_SETTING, is_repo_name
from joshua_core.workspaces_api import DEFAULT_SERVERS_SETTING, parse_default_servers

logger = get_logger("settings_api")

SETTINGS_PATH = "/admin/settings"
# The longest person id a write may name as the writer.
PERSON_MAX = 64

# What a VAPID subject may be (RFC 8292): a mail address or an https URL.
SUBJECT_PREFIXES = ("mailto:", "https://")
# A VAPID public key is one uncompressed P-256 point: the marker 0x04 and the
# two 32-byte coordinates. The private key is the 32-byte private value.
PUBLIC_KEY_BYTES = 65
PRIVATE_KEY_BYTES = 32
UNCOMPRESSED_POINT = 0x04


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _b64url(value: str) -> bytes | None:
    """The bytes of a base64url value, or None when it is not base64url.

    The two VAPID keys are base64url with no padding, as ``py_vapid`` prints
    them, and the padding is added back here.
    """
    text = value.strip()
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def _check_subject(value: str) -> str:
    """``push.vapid_subject``: a ``mailto:`` or an ``https://`` address.

    ``py_vapid`` raises ``VapidException`` on anything else, in the middle of a
    send, so the write is where it is caught instead.
    """
    if value.startswith(SUBJECT_PREFIXES):
        return ""
    return (
        f"{SUBJECT_KEY} must start with mailto: or https://, "
        "as in mailto:you@example.com: a push service writes to it about this instance"
    )


def _check_public_key(value: str) -> str:
    """``push.vapid_public_key``: the 65 uncompressed bytes of a P-256 point."""
    raw = _b64url(value)
    if raw is not None and len(raw) == PUBLIC_KEY_BYTES and raw[0] == UNCOMPRESSED_POINT:
        return ""
    return (
        f"{PUBLIC_KEY} must be the base64url of a VAPID public key: "
        f"{PUBLIC_KEY_BYTES} bytes that start with 0x04. "
        "notifications.md has the command that prints the pair"
    )


def _check_private_key(value: str) -> str:
    """``push.vapid_private_key``: the 32 bytes of a P-256 private value."""
    raw = _b64url(value)
    if raw is not None and len(raw) == PRIVATE_KEY_BYTES:
        return ""
    return (
        f"{PRIVATE_KEY} must be the base64url of a VAPID private key: "
        f"{PRIVATE_KEY_BYTES} bytes. "
        "It is the second line of the command in notifications.md, not the public key"
    )


def _check_update_repo(value: str) -> str:
    """``admin.update_repo``: ``owner/name``, or empty for the default."""
    text = value.strip()
    if not text or is_repo_name(text):
        return ""
    return (
        f"{REPO_SETTING} must be a repository as owner/name, "
        f"such as {DEFAULT_REPO}: the release check reads its releases"
    )


def _check_default_servers(value: str) -> str:
    """``workspaces.default_servers``: MCP server names, separated by a comma.

    Only the form of each name is checked here. A name this instance does not
    declare in its ``mcp`` section, and a server the person who creates the
    workspace cannot use, is dropped from the pre-pick by the options route,
    so the dialog never shows a server picked that the create would refuse.
    """
    if all(re.match(MCP_NAME_PATTERN, name) for name in parse_default_servers(value)):
        return ""
    return (
        f"{DEFAULT_SERVERS_SETTING} must be MCP server names separated by a comma, "
        "such as files, documents: a name is lower case letters, digits and dashes. "
        "A server this instance does not have is left out of the pre-pick"
    )


def _check_skip_approval(value: str) -> str:
    """``developer.pr_skip_approval``: a switch, so a yes, a no, or empty.

    The switch of the page writes ``true`` or ``false``. A word the prompt
    cannot read as either would leave the approval on and say nothing, so it is
    refused here.
    """
    text = value.strip().lower()
    if not text or text in TRUE_WORDS or text in FALSE_WORDS:
        return ""
    return (
        f"{SKIP_APPROVAL_KEY} is a switch: write true or false. "
        "Empty, and anything false, asks the owner before a pull request is opened"
    )


# The check of each setting that has one, by key. A key with no check here
# takes any text the route accepts.
CHECKS: dict[str, Callable[[str], str]] = {
    SUBJECT_KEY: _check_subject,
    PUBLIC_KEY: _check_public_key,
    PRIVATE_KEY: _check_private_key,
    REPO_SETTING: _check_update_repo,
    DEFAULT_SERVERS_SETTING: _check_default_servers,
    SKIP_APPROVAL_KEY: _check_skip_approval,
}


def check_value(key: str, value: str) -> str:
    """Why this value is refused for this key, or ``""`` when it is good.

    A refusal is a line for the owner at the screen: it says what the field
    takes. It never holds the value, which for the private key is a secret.
    """
    check = CHECKS.get(key)
    return check(value) if check is not None else ""


def _field_answer(field: SettingField, stored: Setting | None) -> dict[str, Any]:
    """One field of the answer. A secret value never reaches it: ``value`` is
    null for a secret, and ``is_set`` says whether one is stored."""
    return {
        "key": field.key,
        "label": field.label,
        "hint": field.hint,
        "kind": field.kind,
        "is_secret": field.is_secret,
        "is_set": bool(stored and stored.is_set),
        "value": None if field.is_secret else (stored.value if stored else ""),
        "updated_at": stored.updated_at.isoformat() if stored and stored.updated_at else None,
        "updated_by": stored.updated_by if stored else None,
    }


def _group_answer(group: SettingGroup, rows: dict[str, Setting]) -> dict[str, Any]:
    """One group of the answer: its label and every field it declares, stored
    or not."""
    return {
        "prefix": group.prefix,
        "label": group.label,
        "settings": [_field_answer(field, rows.get(field.key)) for field in group.fields],
    }


def build_settings_router() -> APIRouter:
    router = APIRouter()

    @router.get(SETTINGS_PATH)
    async def get_settings(request: Request):  # type: ignore[no-untyped-def]
        denied = authorize_settings(request)
        if denied is not None:
            return denied
        prefix = request.query_params.get("prefix", "").strip()
        groups = SETTING_GROUPS
        if prefix:
            group = find_group(prefix)
            if group is None:
                return JSONResponse({"error": f"unknown settings prefix {prefix}"}, status_code=404)
            groups = (group,)
        store = request.app.state.ctx.repo.settings
        # ``decrypt=False``: no secret value is read here, so the list answers
        # with no ``JOSHUA_SETTINGS_KEY`` in the environment.
        rows = {
            row.key: row
            for group in groups
            for row in await store.get_many(group.prefix, decrypt=False)
        }
        return JSONResponse({"groups": [_group_answer(group, rows) for group in groups]})

    @router.post(SETTINGS_PATH)
    async def post_setting(request: Request):  # type: ignore[no-untyped-def]
        denied = authorize_settings(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _bad("body must be a JSON object")
        unknown = sorted(set(body) - {"key", "value", "person"})
        if unknown:
            return _bad(f"unknown field: {', '.join(unknown)}")
        key = str(body.get("key") or "").strip()
        if not key or len(key) > KEY_MAX:
            return _bad(f"key must be 1 to {KEY_MAX} characters")
        field = find_field(key)
        if field is None:
            return _bad(f"unknown setting {key}")
        value = body.get("value")
        if not isinstance(value, str):
            return _bad("value must be a string")
        if len(value) > VALUE_MAX:
            return _bad(f"value must be at most {VALUE_MAX} characters")
        if field.is_secret and not value:
            return _bad(f"{key} is a secret: send the new value")
        refusal = check_value(key, value)
        if refusal:
            logger.info({"message": "a setting was refused", "key": key})
            return _bad(refusal)
        person = str(body.get("person") or "")[:PERSON_MAX] or None
        store = request.app.state.ctx.repo.settings
        try:
            stored = await store.set(key, value, is_secret=field.is_secret, updated_by=person)
        except SettingsKeyError as exc:
            logger.warning({"message": "a secret setting was refused", "key": key})
            return JSONResponse({"error": str(exc)}, status_code=503)
        logger.info({"message": "setting written", "key": key, "person": person})
        return JSONResponse({"setting": _field_answer(field, stored)})

    return router

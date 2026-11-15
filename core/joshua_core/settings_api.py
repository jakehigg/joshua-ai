"""``/admin/settings``: the settings of the instance, as the owner edits them.

Core owns the database, so a setting lives here and nowhere else
(``store/settings.py``). The viewer proxies these two routes to the owner
under ``/api/v1/admin/settings/``, as the identity ``viewer``.

- ``GET /admin/settings?prefix=<prefix>`` — the groups of that dotted prefix,
  each with its fields and what is stored for each. With no ``prefix`` it is
  every group. A prefix no group declares is 404.
- ``POST /admin/settings`` — ``{key, value, person?}``: write one setting.
  ``SETTING_GROUPS`` says whether the key is a secret, so a caller can never
  turn a secret into a plain value. A key no group declares is 400.

**No answer carries a secret value.** A field says ``is_secret`` and
``is_set``, and ``value`` is null for a secret, whether it is stored or not.
The owner replaces a secret by writing a new one; nothing reads the old one
back.

A write of a secret needs ``JOSHUA_SETTINGS_KEY`` (see
``store/settings.py``). With the variable unset, that one write is 503 and its
``error`` names the variable; a read of the list and a write of a plain
setting still work.

The caller check is ``authorize_settings``: an admin caller, or one of
``AUTH_CALLERS`` (default ``viewer``). It is checked on both routes with no
exception.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.log import get_logger

from joshua_core.admin import authorize_settings
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

logger = get_logger("settings_api")

SETTINGS_PATH = "/admin/settings"
# The longest person id a write may name as the writer.
PERSON_MAX = 64


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _field_answer(field: SettingField, stored: Setting | None) -> dict[str, Any]:
    """One field of the answer. A secret value never reaches it: ``value`` is
    null for a secret, and ``is_set`` says whether one is stored."""
    return {
        "key": field.key,
        "label": field.label,
        "hint": field.hint,
        "is_secret": field.secret,
        "is_set": bool(stored and stored.is_set),
        "value": None if field.secret else (stored.value if stored else ""),
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
        if field.secret and not value:
            return _bad(f"{key} is a secret: send the new value")
        person = str(body.get("person") or "")[:PERSON_MAX] or None
        store = request.app.state.ctx.repo.settings
        try:
            stored = await store.set(key, value, is_secret=field.secret, updated_by=person)
        except SettingsKeyError as exc:
            logger.warning({"message": "a secret setting was refused", "key": key})
            return JSONResponse({"error": str(exc)}, status_code=503)
        logger.info({"message": "setting written", "key": key, "person": person})
        return JSONResponse({"setting": _field_answer(field, stored)})

    return router

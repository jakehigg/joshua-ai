"""``/people/{person_id}/profile``: the two fields a person edits about themselves.

A person sets ``prompt`` (their own instructions to Joshua) and ``journal``
(consent for what Joshua's journal records about them: ``auto``, ``ask``, or
``off``). Nothing else of the entry is open: not the name, the role, the
handles, or the id.

The viewer calls these routes for the person who signed in, so the caller check
is the one of the ``/auth/*`` routes (``AUTH_CALLERS``, default ``viewer``).
This is no admin action: the viewer sends the id of the signed-in person and
never one from a request body.

``prompt`` is TEXT in the JSON API. In ``joshua.yaml`` and the people file it
is the path of a snippet file, so a write puts the text in
``<data>/people/<id>/prompt.md`` and the people file holds that path (``""``
for no snippet). A read answers the text of the file at the resolved path.

- ``GET`` answers ``{prompt, journal, in_config}``. ``in_config`` is true when
  ``joshua.yaml`` also names the person, so the viewer can say that an edit
  overrides the config file.
- ``POST`` takes ``prompt`` and, or, ``journal``, writes the ``prompt`` and ``journal``
  keys of the person to the people file, and answers the new profile. A person the roster
  does not know is 404.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared import config as config_module
from joshua_shared import layout
from joshua_shared.log import get_logger

from joshua_core.auth_api import require_auth_caller
from joshua_core.store import people_file

logger = get_logger("profile_api")

PROFILE_PATH = "/people/{person_id}/profile"
JOURNAL_CHOICES = ("auto", "ask", "off")
MAX_PROMPT_CHARS = 4000


def _snippet_text(prompt_path: str | None) -> str | None:
    """The stripped text of the snippet file, or None for no path, an empty
    path, a missing file, or one that cannot be read."""
    if not prompt_path:
        return None
    try:
        text = Path(prompt_path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _own_prompt_file(person_id: str) -> Path:
    """The one file a profile write may touch: ``<data>/people/<id>/prompt.md``.

    A path that came from ``joshua.yaml`` is never written.
    """
    return layout.person_root(person_id, config_module.people_file_path().parent) / "prompt.md"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".prompt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _unknown_person() -> JSONResponse:
    return JSONResponse({"error": "unknown person"}, status_code=404)


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _in_config(person_id: str) -> bool:
    """True when ``joshua.yaml`` itself names the person, before the people file."""
    try:
        path = config_module._resolve_path(None)
        # ``parse`` merges no people file when it is given none.
        base = config_module.parse(path.read_text(), source=str(path), file_people=None)
    except (config_module.ConfigError, OSError):
        return False
    return base.person(person_id) is not None


def build_profile_router() -> APIRouter:
    router = APIRouter()

    @router.get(PROFILE_PATH)
    async def read_profile(  # type: ignore[no-untyped-def]
        person_id: str, caller: str = Depends(require_auth_caller)
    ):
        try:
            person = config_module.load().person(person_id)
        except config_module.ConfigError:
            person = None
        if person is None:
            return _unknown_person()
        return JSONResponse(
            {
                "prompt": _snippet_text(person.prompt),
                "journal": person.journal,
                "in_config": _in_config(person_id),
            }
        )

    @router.post(PROFILE_PATH)
    async def write_profile(  # type: ignore[no-untyped-def]
        person_id: str, request: Request, caller: str = Depends(require_auth_caller)
    ):
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        if not isinstance(body, dict):
            return _bad("body must be a JSON object")
        unknown = sorted(set(body) - {"prompt", "journal"})
        if unknown:
            return _bad(f"unknown field: {', '.join(unknown)}")
        if not body:
            return _bad("send prompt, journal, or both")
        try:
            person = config_module.load().person(person_id)
        except config_module.ConfigError:
            person = None
        if person is None:
            return _unknown_person()

        text = _snippet_text(person.prompt)
        prompt_path = person.prompt or ""
        journal = person.journal
        if "journal" in body:
            if body["journal"] not in JOURNAL_CHOICES:
                return _bad("journal must be 'auto', 'ask', or 'off'")
            journal = body["journal"]
        if "prompt" in body:
            value = body["prompt"]
            if value is not None and not isinstance(value, str):
                return _bad("prompt must be a string or null")
            value = (value or "").strip()
            if len(value) > MAX_PROMPT_CHARS:
                return _bad(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
            text = value or None
            prompt_path = str(_own_prompt_file(person_id)) if text else ""

        path = config_module.people_file_path()
        if "prompt" in body:
            own = _own_prompt_file(person_id)
            if text:
                _write_atomic(own, text)
            else:
                own.unlink(missing_ok=True)
        written = people_file.set_profile(path, person_id, prompt=prompt_path, journal=journal)
        if written is None:
            return _unknown_person()
        logger.info({"message": "profile written", "caller": caller, "person": person_id})
        return JSONResponse(
            {"prompt": text, "journal": journal, "in_config": _in_config(person_id)}
        )

    return router

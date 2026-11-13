"""``GET /api/v1/me``: the page of the person who signed in.

The answer holds the person, the address of their profile page in the wiki,
and their most recent attachments (at most ``viewer.HOME_ATTACHMENTS_MAX``).
The anonymous identity is no person on disk, so it gets no profile address
and no attachment.

Each attachment ``url`` is ``/attachments/<path>``, the route the viewer
serves today. Task 131 adds ``GET /api/v1/files/attachments/<path>`` and
moves these links there.

``GET`` and ``POST /api/v1/profile`` read and write two fields of the roster
entry of the person who signed in: ``prompt`` (their own instructions to
Joshua) and ``journal`` (``auto``, ``ask``, or ``off``). Both act on the
signed-in person only, never on an id from a request, and both go through
core (``/people/{id}/profile``). A guest may edit their own profile too.
"""

from __future__ import annotations

import os
from typing import Any

from joshua_shared.config import Person
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_session
from joshua_gateway.viewer_api.edit import _fields, _origin_ok
from joshua_gateway.viewer_api.session import NO_CORE, _answer, person_json


async def me(request: Request, person: Person) -> JSONResponse:
    """The person, their profile address, and their recent attachments."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    anonymous = person.id == viewer.ANONYMOUS_PERSON_ID
    roots = viewer._roots_for(person)
    attachments: list[str] = []
    if not anonymous:
        attachments = viewer._list_attachments(roots["attachments"])[: viewer.HOME_ATTACHMENTS_MAX]
    return JSONResponse(
        {
            "person": person_json(person),
            "profile_url": None if anonymous else f"/wiki/people/{person.id}.md",
            "attachments": [{"path": rel, "url": f"/attachments/{rel}"} for rel in attachments],
        },
        headers=viewer._NO_STORE,
    )


_PROFILE_KEYS = ("prompt", "journal")


def _profile_answer(status: int, body: dict[str, Any]) -> JSONResponse:
    """Core's answer of a profile call, or the error the person should see."""
    if status == 200:
        return _answer({key: body.get(key) for key in (*_PROFILE_KEYS, "in_config")})
    if status in (400, 404):
        message = body.get("error")
        return _answer({"error": message if isinstance(message, str) else "Not found."}, status)
    return _answer({"error": NO_CORE}, 503)


async def profile(request: Request, person: Person) -> JSONResponse:
    """``{prompt, journal, in_config}`` of the signed-in person, from core."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if person.id == viewer.ANONYMOUS_PERSON_ID:
        return _answer({"error": "Not found."}, 404)
    core = viewer_session.from_env(os.environ)
    if core is None:
        return _answer({"error": NO_CORE}, 503)
    status, body = await core.profile(person.id)
    return _profile_answer(status, body)


async def save_profile(request: Request, person: Person) -> JSONResponse:
    """Write ``prompt`` and, or, ``journal`` of the signed-in person through core.

    Only those two keys go on to core. Any other key of the body, an id
    included, is dropped here, and the person is always the one in the session.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    fields = await _fields(request)
    if fields is None or not _origin_ok(request):
        return _answer({"error": "forbidden"}, 403)
    if person.id == viewer.ANONYMOUS_PERSON_ID:
        return _answer({"error": "Not found."}, 404)
    core = viewer_session.from_env(os.environ)
    if core is None:
        return _answer({"error": NO_CORE}, 503)
    wanted = {key: fields[key] for key in _PROFILE_KEYS if key in fields}
    status, body = await core.set_profile(person.id, wanted)
    return _profile_answer(status, body)

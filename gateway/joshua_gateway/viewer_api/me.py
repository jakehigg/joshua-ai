"""``GET /api/v1/me``: the page of the person who signed in.

The answer holds the person, the address of their profile page in the wiki,
and their most recent attachments (at most ``viewer.HOME_ATTACHMENTS_MAX``).
The anonymous identity is no person on disk, so it gets no profile address
and no attachment.

Each attachment ``url`` is ``/attachments/<path>``, the route the viewer
serves today. Task 131 adds ``GET /api/v1/files/attachments/<path>`` and
moves these links there.
"""

from __future__ import annotations

from joshua_shared.config import Person
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.viewer_api.session import person_json


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

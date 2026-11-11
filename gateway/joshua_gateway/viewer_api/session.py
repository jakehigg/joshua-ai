"""``GET /api/v1/session``: who signed in, and the navigation of that person.

The answer holds the person, the sections of the navigation, the chat
address of the person, and the timezone of the instance. The sections come
from ``viewer._sections``, the same source as the server-rendered shell, so
the member rule and the person rule stay in one place.

The links of the sidebar are not here: ``GET /api/v1/links`` is the one
source of the list, and the sidebar reads it (``viewer_api.links``).
"""

from __future__ import annotations

from joshua_shared import config
from joshua_shared.config import Person
from starlette.requests import Request
from starlette.responses import JSONResponse


def person_json(person: Person) -> dict[str, str]:
    """The person of the request, as the React app reads it."""
    return {"id": person.id, "name": person.name, "role": person.role}


async def session(request: Request, person: Person) -> JSONResponse:
    """The session of the person who signed in."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(
        {
            "person": person_json(person),
            "anonymous": person.id == viewer.ANONYMOUS_PERSON_ID,
            "sections": [
                {"key": key, "label": label, "path": url}
                for key, label, url, _ in viewer._sections()
            ],
            "chat_url": f"/chat/{person.id}",
            "timezone": config.load().timezone,
        },
        headers=viewer._NO_STORE,
    )

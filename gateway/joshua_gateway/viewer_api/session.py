"""``GET /api/v1/session``: who signed in, and the navigation of that person.

The answer holds the person, the sections of the navigation, the chat
address of the person, and the timezone of the instance. The table below is
the one source of the sections on the server, and the app holds the same
table with the icons (``src/shell/sections.ts``), so the member rule and the
person rule stay in one place.

The links of the sidebar are not here: ``GET /api/v1/links`` is the one
source of the list, and the sidebar reads it (``viewer_api.links``).
"""

from __future__ import annotations

from joshua_shared import config
from joshua_shared.config import Person, is_member
from starlette.requests import Request
from starlette.responses import JSONResponse

# The sections of the navigation: (key, label, URL).
SECTIONS = (
    ("wiki", "Wiki", "/wiki/"),
    ("journal", "Journal", "/journal"),
    ("search", "Search", "/search"),
    ("usage", "Usage", "/usage"),
    ("assignments", "Assignments", "/assignments"),
    ("documents", "Documents", "/documents"),
    ("chat", "Chat", "/chat"),
)
# The sections that a guest does not get.
MEMBER_SECTIONS = frozenset({"documents"})
# The sections whose URL ends with the id of the person of the request. The
# chat answers at ``/chat/<person id>``, so the item follows the identity of
# the person who reads the viewer.
PERSON_SECTIONS = frozenset({"chat"})


def sections(person: Person) -> list[dict[str, str]]:
    """The sections of a person: a guest gets no Documents.

    A section of ``PERSON_SECTIONS`` gets the id of the person on its URL.
    """
    member = is_member(person.role)
    out: list[dict[str, str]] = []
    for key, label, url in SECTIONS:
        if not member and key in MEMBER_SECTIONS:
            continue
        if key in PERSON_SECTIONS:
            url = f"{url}/{person.id}"
        out.append({"key": key, "label": label, "path": url})
    return out


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
            "sections": sections(person),
            "chat_url": f"/chat/{person.id}",
            "timezone": config.load().timezone,
        },
        headers=viewer._NO_STORE,
    )

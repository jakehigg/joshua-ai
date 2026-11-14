"""The JSON routes of the viewer, which the React app reads.

Every route of this package answers ``application/json``, never caches, and
sits behind the sign-in of a page (``viewer._require_auth``), so a request
with no session cookie gets the same 401 as a page when ``viewer.anonymous``
is off. Each handler takes ``(request, person)``.

Four routes are the exception, because the sign-in page calls them before
there is a session: ``POST session/sign-in``, ``POST session/sign-out``,
``GET auth-method``, and ``GET sign-in/people`` for the ``pin`` method. Their
handlers take the request alone (``viewer_api.session``).

A write route takes a JSON body, and the body carries the CSRF token of its
scope. Every write also checks the ``Origin`` header (``viewer_api.edit``).

The chat has six routes: ``POST /api/v1/chat/<person>/turns`` streams one turn
as server-sent events, ``GET /api/v1/chat/<person>/history`` gives a page
of the conversation, and four ``held`` routes hold the messages written while
a turn still runs. They all go to the channels container (``CHANNELS_URL``).

The workspaces have nine routes under ``/api/v1/workspaces``, which proxy
core (``CORE_URL``). See ``viewer_api.workspaces``.

The Users section has five routes under ``/api/v1/admin/users``, which proxy
core's people and secret routes. They are the owner's alone: a member and a
guest get 403. See ``viewer_api.admin``.

Every path starts with ``API_PREFIX``. The prefix is in one constant and
``api_path`` builds every path from it, so no route can drift from it.

``viewer.build_app`` splices ``routes()`` into its route list, before the
mount of the React app and after the probes.
"""

from __future__ import annotations

from starlette.routing import Route

from joshua_gateway.viewer_api import (
    admin,
    assignments,
    chat,
    documents,
    edit,
    files,
    journal,
    links,
    me,
    search,
    session,
    usage,
    wiki,
    workspaces,
)

# Every JSON route of the viewer is under this prefix, from the first one.
API_PREFIX = "/api/v1"


def api_path(suffix: str) -> str:
    """The full path of one JSON route: ``/api/v1/<suffix>``."""
    return f"{API_PREFIX}/{suffix.strip('/')}"


def routes() -> list[Route]:
    """The JSON routes, each behind the same sign-in as a page.

    The import of ``viewer`` is here, because ``viewer`` imports this package
    to build its app.
    """
    from joshua_gateway.viewer import _require_auth

    return [
        Route(api_path("session"), _require_auth(session.session)),
        # The five routes of the sign-in. Four of them answer with no session,
        # because the sign-in page is what calls them.
        Route(api_path("session/sign-in"), session.sign_in, methods=["POST"]),
        Route(api_path("session/sign-out"), session.sign_out, methods=["POST"]),
        Route(api_path("session/secret"), _require_auth(session.change_secret), methods=["POST"]),
        Route(api_path("auth-method"), session.auth_method),
        Route(api_path("sign-in/people"), session.sign_in_people),
        Route(api_path("me"), _require_auth(me.me)),
        Route(api_path("profile"), _require_auth(me.profile)),
        Route(api_path("profile"), _require_auth(me.save_profile), methods=["POST"]),
        Route(api_path("journal"), _require_auth(journal.feed)),
        Route(api_path("journal/{year:int}/{month:int}/{day:int}"), _require_auth(journal.day)),
        Route(api_path("search"), _require_auth(search.search)),
        Route(api_path("usage"), _require_auth(usage.usage)),
        Route(api_path("links"), _require_auth(links.links)),
        Route(api_path("links"), _require_auth(links.save), methods=["POST"]),
        Route(api_path("documents"), _require_auth(documents.documents)),
        Route(api_path("documents/{owner}/{slug}"), _require_auth(documents.document)),
        Route(
            api_path("documents/{owner}/{slug}/comments"),
            _require_auth(documents.add_comment),
            methods=["POST"],
        ),
        Route(
            api_path("documents/{owner}/{slug}/comments/{cid}/delete"),
            _require_auth(documents.delete_comment),
            methods=["POST"],
        ),
        Route(api_path("assignments"), _require_auth(assignments.assignments)),
        # The saved routes come first: ``saved`` is a name, not an id.
        Route(api_path("assignments/saved"), _require_auth(assignments.saved)),
        Route(api_path("assignments/saved/{id:int}"), _require_auth(assignments.saved_assignment)),
        Route(
            api_path("assignments/saved/{id:int}/comments"),
            _require_auth(assignments.add_saved_comment),
            methods=["POST"],
        ),
        Route(
            api_path("assignments/saved/{id:int}/comments/{cid:int}/delete"),
            _require_auth(assignments.delete_saved_comment),
            methods=["POST"],
        ),
        Route(api_path("assignments/{id:int}"), _require_auth(assignments.assignment)),
        Route(
            api_path("assignments/{id:int}/comments"),
            _require_auth(assignments.add_comment),
            methods=["POST"],
        ),
        Route(
            api_path("assignments/{id:int}/comments/{cid:int}/delete"),
            _require_auth(assignments.delete_comment),
            methods=["POST"],
        ),
        Route(api_path("chat/{person}/turns"), _require_auth(chat.turns), methods=["POST"]),
        Route(api_path("chat/{person}/history"), _require_auth(chat.history)),
        # The hold: the messages written while a turn of the thread still runs.
        # ``take`` comes first, because it is a name and not a hold id.
        Route(api_path("chat/{person}/held/take"), _require_auth(chat.take_held), methods=["POST"]),
        Route(api_path("chat/{person}/held"), _require_auth(chat.held)),
        Route(api_path("chat/{person}/held"), _require_auth(chat.hold), methods=["POST"]),
        Route(
            api_path("chat/{person}/held/{id}"),
            _require_auth(chat.drop_held),
            methods=["DELETE"],
        ),
        Route(api_path("workspaces"), _require_auth(workspaces.list_workspaces)),
        Route(api_path("workspaces"), _require_auth(workspaces.create), methods=["POST"]),
        Route(api_path("workspaces/options"), _require_auth(workspaces.options)),
        Route(api_path("workspaces/{slug}"), _require_auth(workspaces.read)),
        Route(api_path("workspaces/{slug}"), _require_auth(workspaces.change), methods=["PATCH"]),
        Route(api_path("workspaces/{slug}/items"), _require_auth(workspaces.list_items)),
        Route(
            api_path("workspaces/{slug}/items"),
            _require_auth(workspaces.add_item),
            methods=["POST"],
        ),
        Route(
            api_path("workspaces/{slug}/items/{id}"),
            _require_auth(workspaces.change_item),
            methods=["PATCH"],
        ),
        Route(
            api_path("workspaces/{slug}/items/{id}"),
            _require_auth(workspaces.delete_item),
            methods=["DELETE"],
        ),
        # The Users section: the owner's alone, whatever a route looks like.
        Route(api_path("admin/users"), _require_auth(admin.users)),
        Route(api_path("admin/users"), _require_auth(admin.add), methods=["POST"]),
        Route(
            api_path("admin/users/{id}/secret"),
            _require_auth(admin.set_secret),
            methods=["POST"],
        ),
        Route(
            api_path("admin/users/{id}/role"),
            _require_auth(admin.set_role),
            methods=["POST"],
        ),
        Route(
            api_path("admin/users/{id}/remove"),
            _require_auth(admin.remove),
            methods=["POST"],
        ),
        Route(api_path("wiki/tree"), _require_auth(wiki.tree)),
        Route(api_path("wiki"), _require_auth(wiki.content)),
        Route(api_path("files/{root}/{path:path}"), _require_auth(files.file)),
        Route(api_path("edit"), _require_auth(edit.edit_page)),
        Route(api_path("new"), _require_auth(edit.new_page)),
        Route(api_path("save"), _require_auth(edit.save), methods=["POST"]),
        Route(api_path("delete"), _require_auth(edit.delete), methods=["POST"]),
    ]

"""``/v1/workspaces``: the workspaces of a person, and the items in them.

A workspace is a place in Chat for one large piece of work (see
``docs/workspaces.md``). The viewer calls these routes. The caller vouches for
the person with the ``person`` query parameter, as it does for a search of
documents, and every route is scoped to that person: the workspace of another
person answers 404, as one that does not exist.

``workspaces.allowed_callers`` names the fleet identities that may call. An
empty list turns the routes off, so every caller gets 403. The bearer is
checked before the body is read. A ``person`` that is not a member of
``people`` gets 403, and a missing one gets 400.

- ``GET /v1/workspaces?status=`` lists the workspaces, the newest activity in
  the thread first. ``last_activity_at`` is that stamp: core writes it on
  every row of the thread, whoever wrote the row, and ``updated_at`` stays the
  last edit of the workspace. Each workspace, in the list and on its own,
  carries ``turn_in_flight``: a turn of its thread has not finished. The
  viewer composes that with the workers of the workspace into the charm beside
  the name.
- ``POST /v1/workspaces`` creates one (``title``, ``goal``, ``model``,
  ``servers``, ``notify``, ``style``, ``members``). The slug comes from the
  title. The create also makes the channel ``workspace:<id>`` and its
  conversation.
  ``members`` makes it a shared workspace: one thread that the owner and the
  members write in. The slug of a shared workspace is one for everyone, so a
  taken one answers 409 and names it. The people are set here and never
  change: no body and no route adds or removes one. ``shared`` is the
  namespace of a shared workspace, so an owner or a member of that id is 422.
- ``GET`` and ``PATCH /v1/workspaces/{slug}`` read and change one (``title``,
  ``goal``, ``notes``, ``servers``, ``model``, ``notify``, ``style``,
  ``status``). ``{slug}`` may also be the id, or a two-segment ref:
  ``shared/<slug>`` for a shared workspace and ``<owner>/<slug>`` for the
  workspace of a person. A ref with a slash is taken in the path and
  URL-encoded alike.
- ``GET`` and ``POST /v1/workspaces/{slug}/items`` list and add items.
  ``DELETE /v1/workspaces/{slug}/items/{id}`` removes one and
  ``PATCH /v1/workspaces/{slug}/items/{id}`` changes ``pinned`` or ``seen``.
  The assignments container adds the outputs of a run that started in a
  workspace here, with the person of the run, so a run of a person who is not
  one of the people of that workspace adds nothing (404).
- ``GET /v1/workspaces/options`` gives the servers, the models, the writing
  styles, and the people that the person may pick. A model is an ``id`` and
  the ``name`` a person reads. ``default_servers`` is the servers a new
  workspace starts with: the owner's ``workspaces.default_servers`` setting,
  kept to the servers this person may use. Each person of ``people`` carries
  the servers they may use, kept to the servers of this caller, so the dialog
  of a shared workspace pre-picks the servers all of its people can use and
  names no server the instance hides from the caller.
- ``GET /v1/workspaces/{slug}/block`` gives the workspace block as text.
- ``POST /v1/workspaces/{slug}/read`` marks the thread read up to a turn id
  (``turn_id``, the id of a transcript row) for that person. The mark is
  theirs alone, so in a shared workspace every other person keeps the count
  they have. The list route gives each workspace the ``unread`` count of that
  person: the rows of its thread after their mark that somebody else wrote,
  Joshua or another person of the thread. A row of their own never counts.
- ``GET`` and ``POST /v1/workspaces/{slug}/notify`` read and write the notify
  mode of that person in that workspace (``off``, ``decide``, ``all``), which
  governs ``notify_user``. The mode is theirs alone. A person with no row of
  their own has the default: ``decide`` in a shared workspace and ``off`` in a
  personal one.
- ``POST /v1/workspaces/by-id/{id}/items`` is for the gateway, which records
  the items of a tool call from a workspace. It takes the id of the workspace
  and no ``person``: the caller is trusted, and the workspace is the one of the
  stamp. The body is an item and ``added_by`` (``tool:<server>``).

``servers`` must be among the servers every person of the workspace may use,
and every named person must be a member of the roster. ``model`` must be in
``workspaces.models`` when that list is not empty, and any text otherwise. The
routes log the caller and the route, never a goal, a note, or a title.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from joshua_shared.config import is_member
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.gateway_mcp import allowed_gateway_servers
from joshua_shared.item_url import check_item_url
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from joshua_core.engine.workspace import WorkspaceBlock
from joshua_core.store.models import (
    WORKSPACE_ITEM_KINDS,
    WORKSPACE_NOTIFY,
    WORKSPACE_STATUSES,
    WORKSPACE_STYLES,
    Workspace,
    WorkspaceItem,
)
from joshua_core.store.notify import NOTIFY_MODES, default_mode
from joshua_core.store.repo import SHARED_NAMESPACE, SlugTaken

logger = get_logger("workspaces_api")

WORKSPACES_PATH = "/v1/workspaces"
# One workspace, by ref: a slug, an id, or two segments (``shared/<slug>`` or
# ``<owner>/<slug>``). The converter takes the slash, so a two-segment ref
# reaches the route in the path as well as URL-encoded. It is greedy, so every
# route of a longer path stands before these ones in this file.
WORKSPACE_REF_PATH = WORKSPACES_PATH + "/{slug:path}"

# The setting the owner writes on the Instance page: the servers a new
# workspace starts with, as names separated by a comma. Empty picks none.
DEFAULT_SERVERS_SETTING = "workspaces.default_servers"

Notify = Literal["none", "questions", "questions+finishes"]
NotifyMode = Literal["off", "decide", "all"]
Style = Literal["none", "ste", "coach"]
Status = Literal["active", "paused", "archived"]
Kind = Literal[
    "document",
    "artifact",
    "plan",
    "wiki",
    "tasks_project",
    "task",
    "assignment",
    "developer_task",
    "pr",
    "link",
    "file",
]

# The writing styles, as the picker of the viewer shows them: one name and one
# line for each style of ``WORKSPACE_STYLES``, in that order.
STYLES: dict[str, tuple[str, str]] = {
    "none": ("Regular", "Joshua writes as he does everywhere else."),
    "ste": ("Simplified Technical English", "Short sentences, active voice, no hedges."),
    "coach": ("Coach", "Asks what you want to change, and gives one next step."),
}

# The lists above and the store's lists must stay the same.
assert Notify.__args__ == WORKSPACE_NOTIFY  # type: ignore[attr-defined]
assert NotifyMode.__args__ == NOTIFY_MODES  # type: ignore[attr-defined]
assert Style.__args__ == WORKSPACE_STYLES  # type: ignore[attr-defined]
assert tuple(STYLES) == WORKSPACE_STYLES
assert Status.__args__ == WORKSPACE_STATUSES  # type: ignore[attr-defined]
assert Kind.__args__ == WORKSPACE_ITEM_KINDS  # type: ignore[attr-defined]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(_Body):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=4000)
    model: str | None = Field(default=None, max_length=200)
    servers: list[str] = Field(default_factory=list, max_length=100)
    notify: Notify = "none"
    style: Style = "none"
    # The other people of a shared workspace. They are set at the create and
    # never change, so no body below holds them.
    members: list[str] = Field(default_factory=list, max_length=100)


class PatchBody(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)
    servers: list[str] | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    notify: Notify | None = None
    style: Style | None = None
    status: Status | None = None


class ItemBody(_Body):
    kind: Kind
    ref: str = Field(min_length=1, max_length=1000)
    title: str = Field(default="", max_length=500)
    url: str | None = Field(default=None, max_length=2000)

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str | None) -> str | None:
        return check_item_url(value)


class ToolItemBody(ItemBody):
    """An item that the gateway records for a tool call."""

    added_by: str = Field(pattern=r"^tool:[A-Za-z0-9_.-]{1,64}$")


class ItemPatchBody(_Body):
    pinned: bool | None = None
    seen: bool | None = None


class ReadBody(_Body):
    """How far a person has read a thread: the id of a transcript row."""

    turn_id: int = Field(ge=0)


class NotifyModeBody(_Body):
    """When Joshua may notify this person of this workspace."""

    mode: NotifyMode


def _authorize(request: Request) -> tuple[JSONResponse | None, str | None]:
    """Return ``(denial, None)`` or ``(None, identity)``. An empty
    ``allowed_callers`` denies every caller."""
    identity = identify_bearer(request.headers.get("authorization"), load_fleet_tokens())
    if identity is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401), None
    allowed = {n.strip().lower() for n in request.app.state.ctx.settings.workspaces.allowed_callers}
    if identity not in allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403), None
    return None, identity


def _person(request: Request) -> tuple[JSONResponse | None, str | None]:
    """The person the caller vouches for: a member of ``people``."""
    person_id = (request.query_params.get("person") or "").strip()
    if not person_id:
        return JSONResponse({"error": "a person is required"}, status_code=400), None
    person = request.app.state.ctx.settings.person(person_id)
    if person is None or not is_member(person.role):
        return JSONResponse({"error": "unknown person"}, status_code=403), None
    return None, person.id


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not found"}, status_code=404)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def workspace_json(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "slug": workspace.slug,
        "title": workspace.title,
        "owner": workspace.owner,
        "goal": workspace.goal,
        "notes": workspace.notes,
        "servers": list(workspace.servers),
        "model": workspace.model,
        "notify": workspace.notify,
        "style": workspace.style,
        "status": workspace.status,
        "shared": workspace.shared,
        "members": list(workspace.members),
        "channel": workspace.channel_id,
        "created_at": _iso(workspace.created_at),
        "updated_at": _iso(workspace.updated_at),
        "last_activity_at": _iso(workspace.last_activity_at),
    }


def item_json(item: WorkspaceItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "added_by": item.added_by,
        "added_at": _iso(item.added_at),
        "pinned": item.pinned,
        "seen": item.seen,
    }


def _check_servers(
    settings: Any, people: Collection[str], servers: list[str]
) -> JSONResponse | None:
    """422 when a server is not one that every person of the workspace may
    use: the servers are the intersection over all of them.

    The ``detail`` is a sentence for the person at the screen: it names each
    refused server and who in the workspace cannot use it, because the viewer
    shows it as it is."""
    people = list(people)
    reach = {p: set(allowed_gateway_servers(settings, p)) for p in people}
    sentences: list[str] = []
    for server in dict.fromkeys(servers):
        lacking = [p for p in people if server not in reach[p]]
        if not lacking:
            continue
        if len(lacking) == len(people):
            sentences.append(f"This workspace cannot use {server}.")
        else:
            names = _names(settings, lacking)
            sentences.append(f"{names} cannot use {server}.")
    if not sentences:
        return None
    if len(people) > 1:
        sentences.append("A shared workspace can use only the servers that all its people can use.")
    sentences.append("Clear these servers and try again.")
    return JSONResponse(
        {"error": "invalid request", "detail": " ".join(sentences)}, status_code=422
    )


def _names(settings: Any, person_ids: list[str]) -> str:
    """The display names of these people, joined for a sentence."""
    names = [(p.name if (p := settings.person(i)) is not None else i) for i in person_ids]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _check_people(settings: Any, owner: str, members: list[str]) -> JSONResponse | None:
    """422 when a named person is not on the roster, does not write, or holds
    the reserved id ``shared``: that is the namespace of a shared workspace in
    a link and in a ref, so no person may take it."""
    if SHARED_NAMESPACE in (owner, *members):
        return JSONResponse(
            {"error": "invalid request", "detail": f"{SHARED_NAMESPACE} is a reserved person id"},
            status_code=422,
        )
    unknown = sorted(
        m for m in members if (p := settings.person(m)) is None or not is_member(p.role)
    )
    if unknown:
        return JSONResponse(
            {"error": "invalid request", "detail": f"unknown people: {', '.join(unknown)}"},
            status_code=422,
        )
    return None


def _check_model(settings: Any, model: str | None) -> JSONResponse | None:
    """422 when ``workspaces.models`` is set and does not hold ``model``."""
    models = settings.workspaces.models
    if model and models and model not in models:
        return JSONResponse(
            {"error": "invalid request", "detail": f"unknown model: {model}"}, status_code=422
        )
    return None


def workspace_models(settings: Any) -> list[dict[str, str]]:
    """The models a workspace may pick, in the order a person sees them.

    Each one is an ``id``, which the workspace holds and the model call names,
    and a ``name``, which a person reads. ``workspaces.models`` is the list
    when it is set, and a name of it is its id. With no list, the models of
    the instance are the ones of ``assignments.models``, where the key is the
    name a person reads and ``model`` is the id. Two names of one id give one
    entry, the first of them.
    """
    named = settings.assignments.models
    labels = {model.model: name for name, model in reversed(list(named.items()))}
    picked = list(settings.workspaces.models)
    if picked:
        return [{"id": model, "name": labels.get(model, model)} for model in _dedupe(picked)]
    models: list[dict[str, str]] = []
    for name, model in named.items():
        if model.model not in {each["id"] for each in models}:
            models.append({"id": model.model, "name": name})
    return models


def _invalid(exc: ValidationError) -> JSONResponse:
    detail = exc.errors(include_url=False, include_input=False, include_context=False)
    return JSONResponse({"error": "invalid request", "detail": detail}, status_code=422)


def _dedupe(servers: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in servers if s.strip()))


def parse_default_servers(value: str) -> list[str]:
    """The server names of the stored setting, in order and with no duplicate.

    The setting is one line of text, so the names are separated by a comma and
    the spaces around a name do not count.
    """
    return _dedupe(value.split(","))


async def _threads(repo: Any, workspaces: Collection[Workspace]) -> dict[str, str]:
    """The conversation of each workspace thread, by conversation id."""
    threads: dict[str, str] = {}
    for workspace in workspaces:
        conversation = await repo.workspace_conversation(workspace)
        threads[conversation.id] = workspace.id
    return threads


async def _unread(repo: Any, person_id: str, threads: dict[str, str]) -> dict[str, int]:
    """How many rows of each workspace thread this person has not read, by
    workspace id: the rows another speaker wrote, never their own. A workspace
    with nothing unread is not in the answer."""
    counts = await repo.unread_counts(person_id, threads)
    return {threads[conversation_id]: n for conversation_id, n in counts.items()}


def _turns_in_flight(manager: Any, threads: Collection[str]) -> set[str]:
    """The conversations of ``threads`` that hold a turn that has not finished.

    A turn of a workspace thread is one worker Joshua started for that
    workspace, so the viewer shows the workspace as working (``docs/
    workspaces.md``). The turns in flight live in the manager's memory, so
    this costs no query, and a restart of core clears them.
    """
    if manager is None:
        return set()
    return {thread for thread in threads if manager.pending_for(thread) is not None}


def build_workspaces_router() -> APIRouter:
    router = APIRouter()

    async def _scope(
        request: Request, slug: str | None = None
    ) -> tuple[JSONResponse | None, str | None, Workspace | None]:
        """Authorize, name the person, and find the workspace of ``slug``."""
        denied, _ = _authorize(request)
        if denied is not None:
            return denied, None, None
        denied, person_id = _person(request)
        if denied is not None:
            return denied, None, None
        assert person_id is not None
        if slug is None:
            return None, person_id, None
        workspace = await request.app.state.ctx.repo.find_workspace_for(person_id, slug)
        if workspace is None:
            return _not_found(), person_id, None
        return None, person_id, workspace

    @router.get(WORKSPACES_PATH)
    async def list_workspaces(request: Request):  # type: ignore[no-untyped-def]
        denied, person_id, _ = await _scope(request)
        if denied is not None:
            return denied
        status = request.query_params.get("status")
        if status is not None and status not in WORKSPACE_STATUSES:
            return JSONResponse(
                {"error": "invalid request", "detail": "unknown status"}, status_code=422
            )
        ctx = request.app.state.ctx
        repo = ctx.repo
        rows = await repo.list_workspaces(person_id, status)
        threads = await _threads(repo, rows)
        # Each workspace carries the unread count of this person: in a shared
        # workspace the other people have counts of their own.
        unread = await _unread(repo, person_id, threads)
        # And whether a turn of its thread is still running, which the viewer
        # shows as a charm beside the name.
        in_flight = _turns_in_flight(getattr(ctx, "manager", None), threads)
        working = {threads[thread] for thread in in_flight}
        return JSONResponse(
            {
                "workspaces": [
                    {
                        **workspace_json(w),
                        "unread": unread.get(w.id, 0),
                        "turn_in_flight": w.id in working,
                    }
                    for w in rows
                ]
            }
        )

    @router.post(WORKSPACES_PATH)
    async def create_workspace(request: Request):  # type: ignore[no-untyped-def]
        denied, person_id, _ = await _scope(request)
        if denied is not None:
            return denied
        try:
            body = CreateBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        settings = request.app.state.ctx.settings
        servers = _dedupe(body.servers)
        members = _dedupe(body.members)
        refused = (
            _check_people(settings, person_id, members)
            or _check_servers(settings, [person_id, *members], servers)
            or _check_model(settings, body.model)
        )
        if refused is not None:
            return refused
        try:
            workspace = await request.app.state.ctx.repo.create_workspace(
                person_id,
                body.title.strip(),
                goal=body.goal.strip(),
                model=(body.model or "").strip() or None,
                servers=servers,
                notify=body.notify,
                style=body.style,
                members=members,
            )
        except SlugTaken as taken:
            # The slug of a shared workspace is one for everyone, and no number
            # is added: the person picks another title.
            return JSONResponse({"error": str(taken), "slug": taken.slug}, status_code=409)
        logger.info({"message": "workspace created", "workspace": workspace.id})
        return JSONResponse(workspace_json(workspace), status_code=201)

    @router.get(WORKSPACES_PATH + "/options")
    async def get_options(request: Request):  # type: ignore[no-untyped-def]
        """What a person may pick for a workspace: the servers they may use (a
        name and one line), the models of the instance (an ``id`` and the
        ``name`` a person reads), the writing styles, the people they may
        share a workspace with, and the servers a new workspace starts with.

        ``default_servers`` is the owner's setting, kept to the servers this
        person may use: a server they cannot reach is never pre-picked, so the
        dialog does not show it picked and the create refuse it. Each person of
        ``people`` carries the servers they may use, so the dialog intersects
        the pre-pick over the people of a shared workspace, which is the rule
        the create applies (``_check_servers``). Those servers are kept to the
        ones this route shows the caller, so a server the instance hides from
        the caller is named in no entry of ``people``.
        """
        denied, person_id, _ = await _scope(request)
        if denied is not None:
            return denied
        settings = request.app.state.ctx.settings
        reach = allowed_gateway_servers(settings, person_id)
        shown = set(reach)
        servers = [
            {
                "name": name,
                "description": (settings.mcp[name].description or "").strip() or name,
            }
            for name in reach
        ]
        # Everyone on the roster who writes, the caller apart: a guest never
        # holds a workspace. A person's servers are kept to the ones the caller
        # sees, so a server hidden from the caller stays hidden.
        people = [
            {
                "id": person.id,
                "name": person.name,
                "servers": [
                    name for name in allowed_gateway_servers(settings, person.id) if name in shown
                ],
            }
            for person in settings.people
            if person.id != person_id and is_member(person.role)
        ]
        stored = await request.app.state.ctx.repo.settings.get(
            DEFAULT_SERVERS_SETTING, decrypt=False
        )
        default_servers = [
            name
            for name in parse_default_servers(stored.value if stored else "")
            if name in set(reach)
        ]
        return JSONResponse(
            {
                "servers": servers,
                "default_servers": default_servers,
                "models": workspace_models(settings),
                "styles": [
                    {"id": style, "name": name, "description": description}
                    for style, (name, description) in STYLES.items()
                ],
                "people": people,
            }
        )

    @router.post(WORKSPACES_PATH + "/by-id/{workspace_id}/items")
    async def add_tool_item(workspace_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied, identity = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = ToolItemBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        repo = request.app.state.ctx.repo
        workspace = await repo.get_workspace(workspace_id)
        if workspace is None:
            return _not_found()
        item = await repo.add_workspace_item(
            workspace.id,
            body.kind,
            body.ref.strip(),
            body.title.strip() or body.ref.strip(),
            url=(body.url or "").strip() or None,
            added_by=body.added_by,
            # An update with no title keeps the title that the item has.
            rename=bool(body.title.strip()),
        )
        logger.info({"message": "tool item recorded", "caller": identity, "kind": body.kind})
        return JSONResponse(item_json(item))

    @router.get(WORKSPACE_REF_PATH + "/items")
    async def list_items(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        items = await request.app.state.ctx.repo.list_workspace_items(workspace.id)
        return JSONResponse({"items": [item_json(i) for i in items]})

    @router.post(WORKSPACE_REF_PATH + "/items")
    async def add_item(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        try:
            body = ItemBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        item = await request.app.state.ctx.repo.add_workspace_item(
            workspace.id,
            body.kind,
            body.ref.strip(),
            body.title.strip() or body.ref.strip(),
            url=(body.url or "").strip() or None,
            added_by="person",
        )
        return JSONResponse(item_json(item))

    @router.patch(WORKSPACE_REF_PATH + "/items/{item_id}")
    async def patch_item(slug: str, item_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        try:
            body = ItemPatchBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        item = await request.app.state.ctx.repo.update_workspace_item(
            workspace.id, item_id, pinned=body.pinned, seen=body.seen
        )
        return JSONResponse(item_json(item)) if item is not None else _not_found()

    @router.delete(WORKSPACE_REF_PATH + "/items/{item_id}")
    async def delete_item(slug: str, item_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        removed = await request.app.state.ctx.repo.delete_workspace_item(workspace.id, item_id)
        return Response(status_code=204) if removed else _not_found()

    @router.get(WORKSPACE_REF_PATH + "/block")
    async def get_block(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        ctx = request.app.state.ctx
        block = getattr(ctx.manager, "workspace_block", None) or WorkspaceBlock(ctx.repo)
        conversation = await ctx.repo.workspace_conversation(workspace)
        # The same text a turn gets, with no thread rows and no side effect:
        # nothing is marked seen.
        text = await block.render(workspace, conversation.id, fresh=False, mark_seen=None)
        return PlainTextResponse(text)

    @router.post(WORKSPACE_REF_PATH + "/read")
    async def mark_read(slug: str, request: Request):  # type: ignore[no-untyped-def]
        """Mark the thread of this workspace read, up to the id of a transcript
        row, for the person the caller names. The mark is that person's alone:
        in a shared workspace every other person keeps the count they have."""
        denied, person_id, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None and person_id is not None
        try:
            body = ReadBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        repo = request.app.state.ctx.repo
        conversation = await repo.workspace_conversation(workspace)
        read = await repo.mark_conversation_read(person_id, conversation.id, body.turn_id)
        unread = await repo.unread_counts(person_id, [conversation.id])
        return JSONResponse(
            {
                "conversation": conversation.id,
                "last_read_turn_id": read.last_read_turn_id,
                "last_read_at": _iso(read.last_read_at),
                "unread": unread.get(conversation.id, 0),
            }
        )

    @router.get(WORKSPACE_REF_PATH + "/notify")
    async def get_notify_mode(slug: str, request: Request):  # type: ignore[no-untyped-def]
        """The notify mode of this person in this workspace, and the default
        when they have no row of their own."""
        denied, person_id, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None and person_id is not None
        pref = await request.app.state.ctx.repo.notify_prefs.get(person_id, workspace.id)
        default = default_mode(workspace.shared)
        return JSONResponse({"mode": pref.mode if pref else default, "default": default})

    @router.post(WORKSPACE_REF_PATH + "/notify")
    async def set_notify_mode(slug: str, request: Request):  # type: ignore[no-untyped-def]
        """Write the notify mode of this person in this workspace. It is theirs
        alone: nobody sets the mode of another person."""
        denied, person_id, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None and person_id is not None
        try:
            body = NotifyModeBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        pref = await request.app.state.ctx.repo.notify_prefs.set_mode(
            person_id, workspace.id, body.mode
        )
        return JSONResponse({"mode": pref.mode, "default": default_mode(workspace.shared)})

    @router.get(WORKSPACE_REF_PATH)
    async def get_workspace(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        ctx = request.app.state.ctx
        conversation = await ctx.repo.workspace_conversation(workspace)
        in_flight = _turns_in_flight(getattr(ctx, "manager", None), [conversation.id])
        return JSONResponse(
            {**workspace_json(workspace), "turn_in_flight": bool(in_flight)},
        )

    @router.patch(WORKSPACE_REF_PATH)
    async def patch_workspace(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, person_id, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None and person_id is not None
        try:
            body = PatchBody.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        settings = request.app.state.ctx.settings
        fields = body.model_dump(exclude_unset=True)
        if fields.get("title") is None:
            fields.pop("title", None)
        for name in ("goal", "notes", "notify", "style", "status", "servers"):
            if name in fields and fields[name] is None:
                fields.pop(name)
        if "servers" in fields:
            fields["servers"] = _dedupe(fields["servers"])
            refused = _check_servers(settings, workspace.people, fields["servers"])
            if refused is not None:
                return refused
        if "model" in fields:
            fields["model"] = (fields["model"] or "").strip() or None
            refused = _check_model(settings, fields["model"])
            if refused is not None:
                return refused
        for name in ("title", "goal"):
            if name in fields:
                fields[name] = fields[name].strip()
        updated = await request.app.state.ctx.repo.update_workspace(workspace.id, **fields)
        if updated is None:
            return _not_found()
        if "title" in fields:
            await request.app.state.ctx.repo.upsert_channel(
                updated.channel_id,
                "workspace",
                display_name=updated.title,
                default_person_id=updated.owner,
                # The mode of the workspace, so a new title leaves a shared
                # channel shared.
                session_mode="shared" if updated.shared else "per_person",
            )
        return JSONResponse(workspace_json(updated))

    return router

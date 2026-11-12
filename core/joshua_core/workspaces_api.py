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

- ``GET /v1/workspaces?status=`` lists the workspaces.
- ``POST /v1/workspaces`` creates one (``title``, ``goal``, ``model``,
  ``servers``, ``notify``). The slug comes from the title. The create also
  makes the channel ``workspace:<id>`` and its conversation.
- ``GET`` and ``PATCH /v1/workspaces/{slug}`` read and change one (``title``,
  ``goal``, ``notes``, ``servers``, ``model``, ``notify``, ``status``).
  ``{slug}`` may also be the id.
- ``GET`` and ``POST /v1/workspaces/{slug}/items`` list and add items.
  ``DELETE /v1/workspaces/{slug}/items/{id}`` removes one and
  ``PATCH /v1/workspaces/{slug}/items/{id}`` changes ``pinned`` or ``seen``.
- ``GET /v1/workspaces/options`` gives the servers and the models that the
  person may pick.
- ``GET /v1/workspaces/{slug}/block`` gives the workspace block as text.

``servers`` must be among the servers the person may use. ``model`` must be in
``workspaces.models`` when that list is not empty, and any text otherwise. The
routes log the caller and the route, never a goal, a note, or a title.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.gateway_mcp import allowed_gateway_servers
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_core.engine.workspace import WorkspaceBlock
from joshua_core.store.models import (
    WORKSPACE_ITEM_KINDS,
    WORKSPACE_NOTIFY,
    WORKSPACE_STATUSES,
    Workspace,
    WorkspaceItem,
)

logger = get_logger("workspaces_api")

WORKSPACES_PATH = "/v1/workspaces"

Notify = Literal["none", "questions", "questions+finishes"]
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

# The two lists above and the store's lists must stay the same.
assert Notify.__args__ == WORKSPACE_NOTIFY  # type: ignore[attr-defined]
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


class PatchBody(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)
    servers: list[str] | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    notify: Notify | None = None
    status: Status | None = None


class ItemBody(_Body):
    kind: Kind
    ref: str = Field(min_length=1, max_length=1000)
    title: str = Field(default="", max_length=500)
    url: str | None = Field(default=None, max_length=2000)


class ItemPatchBody(_Body):
    pinned: bool | None = None
    seen: bool | None = None


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
    if person is None or person.role != "member":
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
        "status": workspace.status,
        "channel": workspace.channel_id,
        "created_at": _iso(workspace.created_at),
        "updated_at": _iso(workspace.updated_at),
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


def _check_servers(settings: Any, person_id: str, servers: list[str]) -> JSONResponse | None:
    """422 when a server is not one the person may use."""
    allowed = set(allowed_gateway_servers(settings, person_id))
    unknown = sorted({s for s in servers if s not in allowed})
    if unknown:
        return JSONResponse(
            {"error": "invalid request", "detail": f"unknown servers: {', '.join(unknown)}"},
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


def _invalid(exc: ValidationError) -> JSONResponse:
    detail = exc.errors(include_url=False, include_input=False, include_context=False)
    return JSONResponse({"error": "invalid request", "detail": detail}, status_code=422)


def _dedupe(servers: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in servers if s.strip()))


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
        workspace = await request.app.state.ctx.repo.find_workspace(person_id, slug)
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
        rows = await request.app.state.ctx.repo.list_workspaces(person_id, status)
        return JSONResponse({"workspaces": [workspace_json(w) for w in rows]})

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
        refused = _check_servers(settings, person_id, servers) or _check_model(settings, body.model)
        if refused is not None:
            return refused
        workspace = await request.app.state.ctx.repo.create_workspace(
            person_id,
            body.title.strip(),
            goal=body.goal.strip(),
            model=(body.model or "").strip() or None,
            servers=servers,
            notify=body.notify,
        )
        logger.info({"message": "workspace created", "workspace": workspace.id})
        return JSONResponse(workspace_json(workspace), status_code=201)

    @router.get(WORKSPACES_PATH + "/options")
    async def get_options(request: Request):  # type: ignore[no-untyped-def]
        """What a person may pick for a workspace: the servers they may use (a
        name and one line) and the models in ``workspaces.models``. An empty
        ``models`` means any text, and the list is empty."""
        denied, person_id, _ = await _scope(request)
        if denied is not None:
            return denied
        settings = request.app.state.ctx.settings
        servers = [
            {
                "name": name,
                "description": (settings.mcp[name].description or "").strip() or name,
            }
            for name in allowed_gateway_servers(settings, person_id)
        ]
        return JSONResponse({"servers": servers, "models": list(settings.workspaces.models)})

    @router.get(WORKSPACES_PATH + "/{slug}")
    async def get_workspace(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        return JSONResponse(workspace_json(workspace))

    @router.patch(WORKSPACES_PATH + "/{slug}")
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
        for name in ("goal", "notes", "notify", "status", "servers"):
            if name in fields and fields[name] is None:
                fields.pop(name)
        if "servers" in fields:
            fields["servers"] = _dedupe(fields["servers"])
            refused = _check_servers(settings, person_id, fields["servers"])
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
            )
        return JSONResponse(workspace_json(updated))

    @router.get(WORKSPACES_PATH + "/{slug}/items")
    async def list_items(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        items = await request.app.state.ctx.repo.list_workspace_items(workspace.id)
        return JSONResponse({"items": [item_json(i) for i in items]})

    @router.post(WORKSPACES_PATH + "/{slug}/items")
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

    @router.patch(WORKSPACES_PATH + "/{slug}/items/{item_id}")
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

    @router.delete(WORKSPACES_PATH + "/{slug}/items/{item_id}")
    async def delete_item(slug: str, item_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        removed = await request.app.state.ctx.repo.delete_workspace_item(workspace.id, item_id)
        return Response(status_code=204) if removed else _not_found()

    @router.get(WORKSPACES_PATH + "/{slug}/block")
    async def get_block(slug: str, request: Request):  # type: ignore[no-untyped-def]
        denied, _, workspace = await _scope(request, slug)
        if denied is not None:
            return denied
        assert workspace is not None
        ctx = request.app.state.ctx
        block = getattr(ctx.manager, "workspace_block", None) or WorkspaceBlock(ctx.repo)
        conversation = await ctx.repo.find_conversation(workspace.channel_id, workspace.owner)
        # The same text a turn gets, with no thread rows and no side effect:
        # nothing is marked seen.
        text = await block.render(
            workspace, conversation.id if conversation else "", fresh=False, mark_seen=None
        )
        return PlainTextResponse(text)

    return router

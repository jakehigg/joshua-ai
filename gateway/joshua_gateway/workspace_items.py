"""Items for a workspace, from the tool calls of its conversation.

A request from a workspace thread carries ``X-Joshua-Workspace``; the gateway
holds it in ``workspace_ctx`` and does two things with it, in ``proxy.py``:

1. It stamps ``params._meta["joshua/workspace"]`` on the tool call it sends
   upstream, so an addon can store where a long job started and send the
   completion back (``"workspace"`` on ``POST /v1/events``).
2. After a successful call it turns the result into items of the workspace and
   posts them to core (``POST /v1/workspaces/by-id/<id>/items``). This is
   fire and forget: a failure is logged and never reaches the tool call.

The items come from two places. A per-tool map, in code, reads the result of
the tools of Joshua's own servers (the key is the server name and the tool name
as the gateway sees them). Every MCP ``resource_link`` block of any result
becomes a ``link`` item. A tool the map does not know adds nothing.

The recorder needs ``JOSHUA_TOKEN_GATEWAY`` (the fleet token of the gateway)
and the address of core. ``CORE_URL`` names it. Without it, the address is the
service of core on the platform: ``http://joshua-core:8000`` on Kubernetes and
``http://core:8000`` on docker compose. Without the token, no item is recorded
and the gateway says so once in the log.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from joshua_shared.http import FleetClient
from joshua_shared.item_url import is_safe_item_url
from joshua_shared.log import get_logger

logger = get_logger("gateway.workspace_items")

WORKSPACE_META_KEY = "joshua/workspace"
ITEMS_PATH = "/v1/workspaces/by-id/{workspace}/items"
CORE_URL_ENV = "CORE_URL"
TOKEN_ENV = "JOSHUA_TOKEN_GATEWAY"
COMPOSE_CORE_URL = "http://core:8000"
KUBERNETES_CORE_URL = "http://joshua-core:8000"
TIMEOUT_S = 10.0

# The limits of an item row in core (``ItemBody``).
MAX_REF, MAX_TITLE, MAX_URL = 1000, 500, 2000
# The most ``resource_link`` blocks that one result turns into items.
MAX_LINKS = 20


@dataclass(frozen=True)
class ItemSpec:
    """One item to record. An empty ``title`` keeps the title of an item that
    exists, and is the ``ref`` for a new one."""

    kind: str
    ref: str
    title: str = ""
    url: str | None = None


def payload_of(res: Any) -> dict[str, Any] | None:
    """The reply of a tool as a dict: its structured content, else the JSON in
    its first text block. A server that wraps the reply as ``{"result": {...}}``
    is unwrapped."""
    data = getattr(res, "structured_content", None)
    if not isinstance(data, dict):
        data = None
        for block in getattr(res, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.startswith("{"):
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
                break
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    return data if isinstance(data, dict) else None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _first_line(value: Any, limit: int = 120) -> str:
    return _text(next((ln for ln in str(value or "").splitlines() if ln.strip()), ""), limit)


def _document(args: Mapping[str, Any], reply: Mapping[str, Any]) -> list[ItemSpec]:
    path = str(reply.get("path") or "")
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "documents":
        return []
    slug, _, ext = parts[2].rpartition(".")
    kind = "artifact" if ext == "html" else "document"
    return [
        ItemSpec(
            kind,
            path,
            _text(args.get("title"), MAX_TITLE),
            f"/documents/{quote(parts[1])}/{quote(slug)}",
        )
    ]


def _wiki(args: Mapping[str, Any], reply: Mapping[str, Any]) -> list[ItemSpec]:
    path = str(reply.get("path") or "")
    if not path.endswith(".md"):
        return []
    return [ItemSpec("wiki", path, "", f"/wiki/{quote(path)}")]


def _project(args: Mapping[str, Any], reply: Mapping[str, Any]) -> list[ItemSpec]:
    ref = str(reply.get("slug") or reply.get("id") or "")
    return [ItemSpec("tasks_project", ref, _text(reply.get("name") or args.get("name"), MAX_TITLE))]


def _developer(args: Mapping[str, Any], reply: Mapping[str, Any]) -> list[ItemSpec]:
    task = str(reply.get("task_id") or "")
    text = args.get("brief") or args.get("feedback")
    return [ItemSpec("developer_task", task, _first_line(text))]


def _assignment(args: Mapping[str, Any], reply: Mapping[str, Any]) -> list[ItemSpec]:
    number = reply.get("id")
    if isinstance(number, bool) or not isinstance(number, int):
        return []
    return [
        ItemSpec(
            "assignment",
            str(number),
            _text(reply.get("title"), MAX_TITLE),
            f"/assignments/{number}",
        )
    ]


Mapper = Callable[[Mapping[str, Any], Mapping[str, Any]], list[ItemSpec]]

# ``(server, tool)`` as the gateway sees them. The wiki tools are served by the
# ``joshua`` addon; ``wiki`` is the name of an instance that is named for what
# it holds.
TOOL_ITEMS: dict[tuple[str, str], Mapper] = {
    ("documents", "write_document"): _document,
    ("joshua", "write_page"): _wiki,
    ("wiki", "write_page"): _wiki,
    ("tasks", "create_project"): _project,
    ("developer", "develop"): _developer,
    ("developer", "rework"): _developer,
    ("assignments", "start_assignment"): _assignment,
}


def link_items(res: Any) -> list[ItemSpec]:
    """A ``link`` item for each ``resource_link`` block of ``res``. The address
    shows only when it is safe to open: ``http(s)`` or a path that starts with ``/``."""
    items: list[ItemSpec] = []
    for block in getattr(res, "content", None) or []:
        if getattr(block, "type", None) != "resource_link":
            continue
        uri = str(getattr(block, "uri", "") or "").strip()
        if not uri or len(uri) > MAX_REF:
            continue
        name = _text(getattr(block, "name", None), MAX_TITLE)
        url = uri if len(uri) <= MAX_URL and is_safe_item_url(uri) else None
        items.append(ItemSpec("link", uri, name or uri[:MAX_TITLE], url))
        if len(items) >= MAX_LINKS:
            break
    return items


def items_for(server: str, tool: str, args: Mapping[str, Any] | None, res: Any) -> list[ItemSpec]:
    """The items of one successful tool call. A refusal, an error, and a tool
    the map does not know add nothing from the map. A link that an item of the
    map already covers is not added again."""
    if getattr(res, "is_error", False):
        return []
    items: list[ItemSpec] = []
    mapper = TOOL_ITEMS.get((server, tool))
    if mapper is not None:
        reply = payload_of(res)
        if reply is not None and reply.get("status") != "rejected":
            try:
                items = [i for i in mapper(args or {}, reply) if i.ref and len(i.ref) <= MAX_REF]
            except Exception as exc:  # noqa: BLE001 — an odd result shape adds nothing
                logger.warning(
                    {
                        "message": "item map failed",
                        "server": server,
                        "tool": tool,
                        "error": type(exc).__name__,
                    }
                )
    covered = {i.url for i in items if i.url}
    return items + [i for i in link_items(res) if i.ref not in covered]


class ItemRecorder:
    """Post items to core, without blocking the tool call."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport
        self._tasks: set[asyncio.Task[None]] = set()

    def record(self, workspace: str, server: str, items: list[ItemSpec]) -> None:
        """Start the post and return at once. Needs a running event loop."""
        if not items:
            return
        task = asyncio.get_running_loop().create_task(self._post_all(workspace, server, items))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the posts in flight."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _post_all(self, workspace: str, server: str, items: list[ItemSpec]) -> None:
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                for item in items:
                    await self._post(client, workspace, server, item)
        except Exception as exc:  # noqa: BLE001 — recording never fails a tool call
            logger.warning({"message": "workspace items not recorded", "error": type(exc).__name__})

    async def _post(self, client: FleetClient, workspace: str, server: str, item: ItemSpec) -> None:
        body: dict[str, Any] = {
            "kind": item.kind,
            "ref": item.ref,
            "title": item.title,
            "added_by": f"tool:{server}",
        }
        if item.url:
            body["url"] = item.url
        try:
            response = await client.post_json(ITEMS_PATH.format(workspace=workspace), json=body)
        except httpx.HTTPError as exc:
            logger.warning({"message": "workspace item failed", "error": type(exc).__name__})
            return
        if response.status_code != 200:
            logger.warning(
                {
                    "message": "core refused a workspace item",
                    "status": response.status_code,
                    "kind": item.kind,
                    "server": server,
                }
            )


_recorder: ItemRecorder | None = None
_configured = False


def set_recorder(recorder: ItemRecorder | None) -> None:
    """Use ``recorder``, or none. For tests, and for ``main`` to reset."""
    global _recorder, _configured
    _recorder, _configured = recorder, True


def core_url(env: Mapping[str, str]) -> str:
    """``CORE_URL``, else the service of core on this platform."""
    explicit = (env.get(CORE_URL_ENV) or "").strip()
    if explicit:
        return explicit
    return KUBERNETES_CORE_URL if env.get("KUBERNETES_SERVICE_HOST") else COMPOSE_CORE_URL


def recorder_from_env(env: Mapping[str, str]) -> ItemRecorder | None:
    token = env.get(TOKEN_ENV)
    return ItemRecorder(core_url(env), token) if token else None


def get_recorder() -> ItemRecorder | None:
    """The recorder of the process: made from the environment on first use."""
    global _recorder, _configured
    if not _configured:
        _recorder = recorder_from_env(os.environ)
        _configured = True
        if _recorder is None:
            logger.info({"message": f"workspace items are off: {TOKEN_ENV} is not set"})
    return _recorder


def on_result(
    server: str, tool: str, args: Mapping[str, Any] | None, res: Any, workspace: str
) -> None:
    """The hook of ``proxy.py``: record the items of a call from a workspace."""
    recorder = get_recorder()
    if recorder is None:
        return
    items = items_for(server, tool, args, res)
    if items:
        recorder.record(workspace, server, items)

"""The ``workspaces`` source adapter: one record for each person of each active
workspace.

The record holds the title, the goal, the notes, and the titles of the items.
The indexer writes one row (kind ``workspace``) with the title as the title,
the status as the heading, and this text, cut to ``TEXT_CHARS``. The row is
scoped to one person, so a shared workspace has one record for each of its
people and each of them finds it. A paused or archived workspace has no row.

Normal chat reads the row for one line, "May relate to workspace X"
(``engine/workspace_awareness.py``). It is never shown as text, and recall and
the RAG note never return it. The uri is ``workspaces/<id>/<person>``. The
revision is a hash of the record, so the indexing cycle writes a row again only
when the workspace changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from joshua_core.memory.sources import Document
from joshua_core.store.models import Workspace, WorkspaceItem

WORKSPACES_SOURCE = "workspaces"
WORKSPACES_PREFIX = "workspaces/"
# The text of a record holds at most this many characters.
TEXT_CHARS = 1500
# At most this many item titles go in the record.
MAX_ITEM_TITLES = 30


def uri_of(workspace_id: str, person_id: str) -> str:
    return f"{WORKSPACES_PREFIX}{workspace_id}/{person_id}"


def _segments(uri: str) -> list[str]:
    if not uri.startswith(WORKSPACES_PREFIX):
        return []
    return uri[len(WORKSPACES_PREFIX) :].split("/")


def workspace_id_of_uri(uri: str) -> str | None:
    """The workspace of a record uri: the second segment of
    ``workspaces/<id>/<person>``."""
    parts = _segments(uri)
    return parts[0] if parts and parts[0] else None


def person_of_uri(uri: str) -> str | None:
    """The person of a record uri: the third segment."""
    parts = _segments(uri)
    return parts[1] if len(parts) > 1 and parts[1] else None


def record_text(workspace: Workspace, items: list[WorkspaceItem]) -> str:
    """The text of the record: goal, notes, and item titles, on short lines."""
    parts = []
    if workspace.goal:
        parts.append(f"Goal: {workspace.goal}")
    if workspace.notes:
        parts.append(f"Notes: {workspace.notes}")
    titles = [i.title or i.ref for i in items[:MAX_ITEM_TITLES]]
    if titles:
        parts.append("Items: " + "; ".join(titles))
    text = "\n".join(" ".join(p.split()) for p in parts)
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS].rstrip() + "…"


def _document(workspace: Workspace, person_id: str, items: list[WorkspaceItem]) -> Document:
    text = record_text(workspace, items)
    rev = hashlib.sha256(
        "\x00".join((workspace.title, workspace.status, text)).encode()
    ).hexdigest()
    return Document(
        source=WORKSPACES_SOURCE,
        uri=uri_of(workspace.id, person_id),
        person_id=person_id,
        title=workspace.title,
        text=text,
        updated_at=workspace.updated_at or datetime.now(UTC),
        rev=rev,
        frontmatter={"kind": "workspace", "status": workspace.status},
    )


class WorkspacesSource:
    """Lists the active workspaces from the database, one record per person."""

    name = WORKSPACES_SOURCE

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def list_documents(self) -> AsyncIterator[Document]:
        for workspace in await self._repo.list_all_workspaces("active"):
            items = await self._repo.list_workspace_items(workspace.id)
            for person_id in workspace.people:
                yield _document(workspace, person_id, items)

    async def fetch(self, uri: str) -> Document | None:
        workspace_id, person_id = workspace_id_of_uri(uri), person_of_uri(uri)
        workspace = await self._repo.get_workspace(workspace_id) if workspace_id else None
        if workspace is None or workspace.status != "active":
            return None
        if person_id is None or person_id not in workspace.people:
            return None
        return _document(workspace, person_id, await self._repo.list_workspace_items(workspace.id))

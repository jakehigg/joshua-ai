"""The item that one recorded output makes in the workspace of a run.

An assignment that starts in a workspace thread keeps the id of that
workspace in its spec (``manager.stamp_workspace``). Each output the run
records with ``record_output`` becomes an item of that workspace, at the
moment it is recorded, so the person finds what the run wrote in the Items
panel of the thread and nobody adds it by hand.

The kind of the item is the kind of the output when core knows that kind, and
``link`` otherwise. An output that names a document
(``documents/<owner>/<slug>.md``) is a ``document``, ``.html`` an
``artifact``, and the url is the viewer page of the document, so the item
opens inside the chat. An output whose ref is an ``http(s)`` address keeps it
as the url.

``CoreClient.add_workspace_item`` writes the row. Core takes the person the
run belongs to, so an output of a run whose person is not one of the people
of the workspace adds nothing. An item with the same kind and ref updates the
one row it has, so the same output recorded twice is one item.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from joshua_assignments.report import document_page

# The kinds of item that core accepts (core: store/models.py).
ITEM_KINDS = (
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
)
# An output that names no kind core knows is a link to what it points at.
OTHER_KIND = "link"
# The limits of an item row in core (``ItemBody``).
MAX_REF, MAX_TITLE, MAX_URL = 1000, 500, 2000

_URL_RE = re.compile(r"^https?://[^\s/]+\S*$")


@dataclass(frozen=True)
class Item:
    """One item of a workspace, as core's items route takes it."""

    kind: str
    ref: str
    title: str = ""
    url: str | None = None

    def body(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "title": self.title, "url": self.url}


def item_of(output: Mapping[str, Any]) -> Item | None:
    """The item of one recorded output, or None when it names nothing."""
    ref = str(output.get("ref") or "").strip()
    if not ref or len(ref) > MAX_REF:
        return None
    kind = str(output.get("kind") or "").strip().lower()
    title = " ".join(str(output.get("title") or "").split())[:MAX_TITLE]
    page = document_page(ref)
    url: str | None = None
    if page is not None:
        kind = "artifact" if ref.endswith(".html") else "document"
        url = page[:MAX_URL]
    elif _URL_RE.match(ref):
        url = ref[:MAX_URL]
    return Item(kind if kind in ITEM_KINDS else OTHER_KIND, ref, title, url)

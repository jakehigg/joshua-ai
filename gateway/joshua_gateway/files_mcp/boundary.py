"""The boundary between the wiki and the documents, in one place.

The wiki is what Joshua knows, written as reference. A plan, a draft, a
report, a spec, or an artifact is a document: it lives under ``documents/``,
it changes, and it is not recall. ``check_boundary`` is the one rule both MCP
servers apply before a write:

- ``write_file`` (and ``promote_document``, which writes the wiki through the
  same code) refuses a wiki page whose frontmatter has ``kind`` ``plan``,
  ``report``, ``spec``, ``artifact`` or ``draft``, or ``status: draft``.
- ``write_document`` refuses a path under ``wiki/``.

Each message is safe to return to the caller.
"""

from __future__ import annotations

from typing import Any

import yaml

# The kinds of a page that is a document, not a wiki page.
DOCUMENT_KINDS = frozenset({"plan", "report", "spec", "artifact", "draft"})

WIKI_REFUSAL = "a plan or a draft is a document: use write_document"
DOCUMENT_REFUSAL = (
    "write_document never writes the wiki: a document path is documents/<owner>/<slug>.md. "
    "Write a wiki page with write_file, or promote a finished document with promote_document"
)


class BoundaryError(Exception):
    """A write that crosses the boundary. The message is safe to return."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def frontmatter(content: str) -> dict[str, Any]:
    """The leading ``---`` YAML block of ``content`` as a dict, or ``{}``.

    A block that does not parse, or that is not a mapping, gives ``{}``: the
    files server refuses invalid YAML with its own message.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def is_document_shaped(meta: dict[str, Any]) -> bool:
    """True when frontmatter ``meta`` says the text is a document."""
    kind = str(meta.get("kind", "")).strip().lower()
    status = str(meta.get("status", "")).strip().lower()
    return kind in DOCUMENT_KINDS or status == "draft"


def check_boundary(path: str, content: str, *, tool: str) -> None:
    """Refuse a write that crosses the wiki boundary. Raises ``BoundaryError``.

    ``path`` is the root-relative path of the write, ``content`` the whole text
    that will be on disk, and ``tool`` the tool that writes. ``write_document``
    never writes under ``wiki/``. Every other tool that writes under ``wiki/``
    never writes a page whose frontmatter says it is a document.
    """
    in_wiki = path.strip().startswith("wiki/") or path.strip() == "wiki"
    if not in_wiki:
        return
    if tool == "write_document":
        raise BoundaryError(DOCUMENT_REFUSAL)
    if is_document_shaped(frontmatter(content)):
        raise BoundaryError(WIKI_REFUSAL)

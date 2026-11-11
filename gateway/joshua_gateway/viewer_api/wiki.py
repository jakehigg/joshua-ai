"""``GET /api/v1/wiki/tree``: the wiki as nested JSON.

The tree comes from ``viewer_wiki.build_tree``, the one traversal of the
wiki, so this route adds no second walk of the folder. Each node is
``{"title", "rel", "url", "count", "children"}``: a folder has its pages and
its subfolders as children, and a page has none. ``count`` is how many pages
and files the folder holds, down the whole tree, and 0 for a page: the app
shows it beside the folder and cannot count the files, because a file that
is not a page is not a node. The front page of the wiki is the first child
of the root. The journal is a feed, not a tree, so its folder is one node
with no child and the address ``/journal``.
"""

from __future__ import annotations

from typing import Any

from joshua_shared.config import Person
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_wiki as wiki_mod

# The answer for a data volume with no wiki folder yet.
EMPTY_TREE: dict[str, Any] = {
    "title": "wiki",
    "rel": "",
    "url": "/wiki/",
    "count": 0,
    "children": [],
}


def _page_node(page: wiki_mod.Page) -> dict[str, Any]:
    return {"title": page.title, "rel": page.rel, "url": page.url, "count": 0, "children": []}


def _folder_node(folder: wiki_mod.Folder) -> dict[str, Any]:
    if folder.name == wiki_mod.JOURNAL_DIR:
        return {
            "title": "journal",
            "rel": folder.rel,
            "url": "/journal",
            "count": folder.count(),
            "children": [],
        }
    children = [_folder_node(child) for child in folder.folders]
    children += [_page_node(page) for page in folder.pages]
    return {
        "title": folder.title,
        "rel": folder.rel,
        "url": folder.url,
        "count": folder.count(),
        "children": children,
    }


def tree_json(root: wiki_mod.Folder) -> dict[str, Any]:
    """The whole wiki as nested JSON, the front page first."""
    node = _folder_node(root)
    if root.index is not None:
        node["children"].insert(0, _page_node(root.index))
    return node


async def tree(request: Request, person: Person) -> JSONResponse:
    """The wiki tree the person reads."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    folder = wiki_mod.build_tree(viewer._roots_for(person)["wiki"].base)
    return JSONResponse(
        tree_json(folder) if folder is not None else EMPTY_TREE, headers=viewer._NO_STORE
    )

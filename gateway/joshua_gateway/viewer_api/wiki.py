"""The JSON routes of the wiki: one address, and the whole tree.

``GET /api/v1/wiki?path=<root-relative path>`` answers one of three shapes,
by what the path names:

- ``page``: a Markdown file, with its title, its frontmatter, its body, the
  crumbs above it, the other pages of its folder, and, for a member on the
  wiki, the address of the editor and the CSRF token of the delete.
- ``folder``: a folder of the wiki, with the body of its index page and what
  is in it.
- ``file``: anything else, with the address that delivers the bytes
  (``viewer_api.files``). The app sends the browser there.

The path starts with a root name (``wiki/recipes/pizza.md``,
``shared/team.jpg``, ``attachments/2026/11/a.jpg``) and goes through the
resolver of the files MCP, so a path that escapes its root is 404.

``GET /api/v1/wiki/tree`` is the wiki as nested JSON.

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

from pathlib import Path
from typing import Any

from joshua_shared.config import Person, is_member
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_wiki as wiki_mod
from joshua_gateway.files_mcp.paths import PathError, Root, resolve
from joshua_gateway.files_mcp.server import IMAGE_MIME
from joshua_gateway.viewer_api.edit import edit_url, new_url
from joshua_gateway.viewer_api.files import file_url

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


# -- one address ------------------------------------------------------------


def _answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _not_found() -> JSONResponse:
    return _answer({"error": "not found"}, status=404)


def page_json(root: Root, abs_path: Path, rel: str, person: Person, text: str) -> dict[str, Any]:
    """One Markdown file: its title, its frontmatter, its body, and the
    navigation around it.

    A page of the wiki gets its own title, the crumbs above it and the other
    pages of its folder. A page of another root is one file, so it keeps the
    path as its title and gets no neighbour. A member on the wiki also gets
    the editor and the token of the delete.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    front, body = viewer._split_frontmatter(text)
    in_wiki = root.name == "wiki"
    under = abs_path.relative_to(root.base).as_posix()
    title = rel
    crumbs: list[dict[str, str]] = []
    siblings: list[dict[str, str]] = []
    if in_wiki:
        title = wiki_mod.page_title(text, wiki_mod.stem_title(abs_path.stem))
        folder_rel = under.rsplit("/", 1)[0] if "/" in under else ""
        crumbs = wiki_mod.crumbs(root.base, folder_rel, leaf=title)
        siblings = wiki_mod.siblings(root.base, under)
    can_edit = in_wiki and is_member(person.role)
    return {
        "kind": "page",
        "root": root.name,
        "rel": rel,
        "title": title,
        "frontmatter": wiki_mod.meta_pairs(front or {}),
        "markdown": body,
        "crumbs": crumbs,
        "siblings": siblings,
        "can_edit": can_edit,
        "edit_url": edit_url(under) if in_wiki else "",
        "delete_csrf": viewer._csrf_token(person.id, rel) if can_edit else "",
    }


def folder_json(root: Root, rel: str, person: Person) -> dict[str, Any] | None:
    """One folder of the wiki: the body of its index page, then what is in it.

    ``rel`` is wiki-relative, ``""`` for the whole wiki. The index page of a
    folder sits beside it, one folder up, so its body resolves its own
    relative links there. None when ``rel`` is no folder of the wiki.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    folder = wiki_mod.build_tree(root.base, rel)
    if folder is None:
        return None
    index_markdown = None
    if folder.index is not None:
        text = viewer._read_text(root.base / folder.index.rel) or ""
        _, body = viewer._split_frontmatter(text)
        _, index_markdown = wiki_mod.first_heading(body)  # the folder title is the h1
    folders = [
        {"title": child.title, "url": child.url}
        for child in folder.folders
        if child.name != wiki_mod.JOURNAL_DIR or folder.rel
    ]
    if not folder.rel and any(c.name == wiki_mod.JOURNAL_DIR for c in folder.folders):
        # The journal is a feed, not a tree, so the root links the feed.
        folders.insert(0, {"title": "journal", "url": "/journal"})
    return {
        "kind": "folder",
        "rel": folder.rel,
        "title": folder.title,
        "crumbs": wiki_mod.crumbs(root.base, rel, leaf=None) if rel else [],
        "index_markdown": index_markdown,
        "folders": folders,
        "pages": [{"title": page.title, "url": page.url} for page in folder.pages],
        "files": [{"name": f.name, "url": file_url(root.name, f.rel)} for f in folder.files],
        "can_create": is_member(person.role),
        "new_url": new_url(folder.rel),
    }


def file_json(root: Root, abs_path: Path) -> dict[str, Any]:
    """One file that is not a page: where its bytes are, and whether a browser
    shows them inline."""
    under = abs_path.relative_to(root.base).as_posix()
    return {
        "kind": "file",
        "rel": under,
        "name": abs_path.name,
        "url": file_url(root.name, under),
        "inline": abs_path.suffix.lower() in IMAGE_MIME,
    }


async def content(request: Request, person: Person) -> JSONResponse:
    """The page, the folder, or the file at ``?path=``.

    A folder is a folder of the wiki only: the other roots hold files, so a
    directory there is 404, as it is for a path that escapes its root and for
    a file that is not there. A Markdown file the viewer cannot read as text
    is a file, so it downloads.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    rel = request.query_params.get("path") or ""
    try:
        root, abs_path = resolve(rel, viewer._roots_for(person), write=False)
    except PathError:
        return _not_found()
    if abs_path.is_dir():
        if root.name != "wiki":
            return _not_found()
        under = abs_path.relative_to(root.base).as_posix()
        folder = folder_json(root, "" if under == "." else under, person)
        return _not_found() if folder is None else _answer(folder)
    if not abs_path.is_file():
        return _not_found()
    if abs_path.suffix.lower() == ".md":
        text = viewer._read_text(abs_path)
        if text is not None:
            return _answer(page_json(root, abs_path, rel, person, text))
    return _answer(file_json(root, abs_path))

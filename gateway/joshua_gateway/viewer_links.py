"""The viewer's links: a short list of external links in the navigation.

A member keeps the list at ``/links``. Each link has a title, an http(s) URL
and an icon from a small built-in set, and opens in a new tab. The list lives
in one small file, ``wiki/.viewer/links.yaml``. The dot directory keeps it out
of the wiki tree, the search and the memory index (``layout.is_hidden``), and
the wiki's git repository still records each change.

The icons are built in because the viewer's CSP allows images from ``'self'``
only, so a link cannot bring its own favicon.

This module has no routes. ``viewer_api.links`` serves the list and the write;
this module reads, checks and writes the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from joshua_shared.log import get_logger

logger = get_logger("viewer.links")

# The file, under the wiki root.
LINKS_DIR = ".viewer"
LINKS_FILE = "links.yaml"

# Limits: the list stays a short navigation list, and the file stays tiny.
MAX_LINKS = 30
MAX_TITLE = 40
MAX_URL = 500

DEFAULT_ICON = "link"

# The built-in icons: name -> SVG path data, on a 24px grid, drawn with the
# stroke of ``.j-icon``. The name is what the file stores.
ICONS: dict[str, str] = {
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
    '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "box": '<path d="m3 7 9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7M12 11v10"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/>'
    '<path d="m3.5 6 1 1 2-2M3.5 12l1 1 2-2M3.5 18l1 1 2-2"/>',
    "cart": '<path d="M3 4h2l2.5 11h10L20 8H6.5"/><circle cx="9" cy="19" r="1.3"/>'
    '<circle cx="17" cy="19" r="1.3"/>',
    "chart": '<path d="M4 20V11M10 20V4M16 20v-8M21 20H3"/>',
    "home": '<path d="m3 11 9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "music": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/>'
    '<circle cx="17" cy="16" r="3"/>',
    "calendar": '<path d="M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5'
    'a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/>'
    '<path d="M8 2v4M16 2v4M3 10h18"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>'
    '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "tool": '<path d="M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-3-3 6.7-6.7a4 4 0 0 1-5-5z"/>'
    '<path d="m3 21 6-6"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "cloud": '<path d="M7 18a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10a4 4 0 0 1 0 8z"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/>'
    '<rect x="3" y="13" width="18" height="7" rx="2"/>'
    '<path d="M7 7.5h.01M7 16.5h.01"/>',
    "camera": '<path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4'
    'a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/>'
    '<circle cx="12" cy="13" r="4"/>',
    "chat": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    "heart": '<path d="M12 20s-7-4.4-9-9a5 5 0 0 1 9-3 5 5 0 0 1 9 3c-2 4.6-9 9-9 9z"/>',
    "code": '<path d="m8 8-5 4 5 4M16 8l5 4-5 4M14 4l-4 16"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/>',
    "globe": '<circle cx="12" cy="12" r="9"/>'
    '<path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "play": '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
}


class LinkError(ValueError):
    """A link of a write that cannot be saved. The message is for the person."""


@dataclass(frozen=True)
class Link:
    title: str
    url: str
    icon: str = DEFAULT_ICON


def links_path(wiki_base: Path) -> Path:
    return wiki_base / LINKS_DIR / LINKS_FILE


def check_url(raw: str) -> str:
    """Return ``raw`` stripped when it is an absolute http(s) URL with a host.

    Anything else (``javascript:``, ``data:``, a relative path) is refused, so a
    link in the navigation can only open a web page.
    """
    url = raw.strip()
    if not url:
        raise LinkError("a link needs a URL")
    if len(url) > MAX_URL:
        raise LinkError(f"a URL can be at most {MAX_URL} characters")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise LinkError("a URL cannot contain spaces")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise LinkError("that is not a valid URL") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise LinkError("a URL must start with http:// or https:// and name a host")
    return url


def check_link(title: str, url: str, icon: str) -> Link:
    """One link, checked. Raises ``LinkError`` with a message for the person."""
    clean_title = " ".join(title.split())
    if not clean_title:
        raise LinkError("a link needs a title")
    if len(clean_title) > MAX_TITLE:
        raise LinkError(f"a title can be at most {MAX_TITLE} characters")
    return Link(
        title=clean_title,
        url=check_url(url),
        icon=icon if icon in ICONS else DEFAULT_ICON,
    )


def parse(text: str) -> list[Link]:
    """The links of a ``links.yaml`` text. A bad entry is skipped and logged,
    so one hand-edit mistake never hides the rest of the list."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.warning({"message": "links file is not valid YAML; showing no links"})
        return []
    items: Any = doc.get("links") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return []
    links: list[Link] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            links.append(
                check_link(
                    str(item.get("title") or ""),
                    str(item.get("url") or ""),
                    str(item.get("icon") or DEFAULT_ICON),
                )
            )
        except LinkError as exc:
            logger.warning({"message": "links file entry skipped", "reason": str(exc)})
        if len(links) == MAX_LINKS:
            break
    return links


def load(wiki_base: Path) -> list[Link]:
    """The saved links, or none when the file is missing or unreadable."""
    try:
        text = links_path(wiki_base).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return parse(text)


def dump(links: list[Link]) -> str:
    """The ``links.yaml`` text for ``links``."""
    body = yaml.safe_dump(
        {"links": [{"title": ln.title, "url": ln.url, "icon": ln.icon} for ln in links]},
        sort_keys=False,
        allow_unicode=True,
    )
    return (
        "# The links in the viewer's navigation. Edit them at /links in the viewer.\n"
        "# Each link: title, url (http or https), icon (a built-in icon name).\n" + body
    )


def from_rows(rows: Any) -> list[Link]:
    """The links of the rows of a write, in the order the person put them.

    One row is an object with ``title``, ``url`` and ``icon``. A row with no
    title and no URL is dropped, so a blank row of the editor is not an
    error. Any other row must be a valid link, and its icon must be a
    built-in one: the first error raises ``LinkError`` and names the row.
    """
    if not isinstance(rows, list):
        raise LinkError("the write carries no links")
    links: list[Link] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise LinkError(f"row {index + 1}: that is not a link")
        title = str(row.get("title") or "")
        url = str(row.get("url") or "")
        icon = str(row.get("icon") or DEFAULT_ICON)
        if not title.strip() and not url.strip():
            continue
        if icon not in ICONS:
            raise LinkError(f"row {index + 1}: {icon} is not a built-in icon")
        try:
            links.append(check_link(title, url, icon))
        except LinkError as exc:
            raise LinkError(f"row {index + 1}: {exc}") from exc
    if len(links) > MAX_LINKS:
        raise LinkError(f"keep at most {MAX_LINKS} links")
    return links

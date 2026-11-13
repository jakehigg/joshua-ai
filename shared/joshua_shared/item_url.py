"""The address of a workspace item: one rule for every entry point.

An item address ends up in an ``href`` of the viewer, so only an ``http`` or
``https`` address and a relative path that starts with ``/`` are allowed. A
scheme such as ``javascript:`` or ``data:`` is not. So is a path that starts
with ``//``, which a browser reads as an address of another host.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def is_safe_item_url(url: str) -> bool:
    """True for an ``http(s)`` address with a host, or a path that starts with ``/``."""
    text = (url or "").strip()
    if not text or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text) or "\\" in text:
        return False
    if text.startswith("/"):
        return not text.startswith("//")
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def check_item_url(url: str | None) -> str | None:
    """The trimmed address, or None for an empty one. Raises ``ValueError`` for
    an address that is not allowed."""
    text = (url or "").strip()
    if not text:
        return None
    if not is_safe_item_url(text):
        raise ValueError("url must be an http(s) address or a path that starts with /")
    return text

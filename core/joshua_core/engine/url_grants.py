"""Which URLs the agent may hand to the research worker.

A person sends a link and says "save this recipe". Research answers with its
sources, and the person says "look at that second page again". Both need the
agent to name a URL, so `research_web` takes them.

That is also the shortest way out of this instance. A page the worker read, an
attachment somebody sent, or a tool result can all carry words that tell the
agent to fetch `https://somewhere/?data=<something private>`. The block list
does not stop that: the host is on the open web, which is exactly where a fetch
is allowed to go.

So a URL has to be **granted** before the agent may pass it:

- **A person wrote it.** Every URL in the text of an inbound turn is granted
  for that conversation. A person who sends a link has asked for it to be read.
- **Research found it.** Every source of an answer is granted, so a follow-up
  question about one of them works.

Nothing else is. A URL that reaches the agent from a page, from the text of an
attachment, or from the model itself is not granted, and the tool refuses it
and says why. The agent can still ask the person for the link.

The grants live for as long as core runs and are bounded for each conversation.
They are a gate on what the agent may ask for, not a record of anything.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit

# A URL as a person writes one in a message. Trailing punctuation is dropped by
# the strip below, so "see https://example.com/x." keeps the page and not the
# full stop.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_TRAILING = ".,;:!?)]}>\"'"

# How many URLs one conversation keeps, and how many conversations are held.
MAX_PER_CONVERSATION = 64
MAX_CONVERSATIONS = 256


def find_urls(text: str) -> list[str]:
    """Every URL in `text`, in the order they appear, without duplicates."""
    found: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_TRAILING)
        if url and url not in found:
            found.append(url)
    return found


def canonical(url: str) -> str:
    """The form two URLs are compared by: scheme, host, port, path, and query.

    The fragment goes, because it never reaches a server, and the host is
    lowered. Nothing else is touched: a query string is part of which page this
    is, and dropping it would grant more than the person did.
    """
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


class UrlGrants:
    """The URLs each conversation has been given, and may ask to have read."""

    def __init__(
        self,
        max_per_conversation: int = MAX_PER_CONVERSATION,
        max_conversations: int = MAX_CONVERSATIONS,
    ) -> None:
        self._per_conversation = max_per_conversation
        self._max_conversations = max_conversations
        self._grants: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()

    def grant(self, conversation_id: str, urls: list[str]) -> int:
        """Grant `urls` for one conversation. Returns how many are new."""
        if not conversation_id or not urls:
            return 0
        held = self._grants.get(conversation_id)
        if held is None:
            held = OrderedDict()
            self._grants[conversation_id] = held
            while len(self._grants) > self._max_conversations:
                self._grants.popitem(last=False)
        self._grants.move_to_end(conversation_id)

        added = 0
        for url in urls:
            key = canonical(url)
            if not key or key in held:
                continue
            held[key] = url
            added += 1
            while len(held) > self._per_conversation:
                held.popitem(last=False)
        return added

    def grant_from_text(self, conversation_id: str, text: str) -> int:
        """Grant every URL a person wrote in one message."""
        return self.grant(conversation_id, find_urls(text))

    def is_granted(self, conversation_id: str, url: str) -> bool:
        held = self._grants.get(conversation_id)
        return bool(held) and canonical(url) in held

    def granted(self, conversation_id: str) -> list[str]:
        """Every URL this conversation may ask to have read, newest last."""
        return list((self._grants.get(conversation_id) or {}).values())

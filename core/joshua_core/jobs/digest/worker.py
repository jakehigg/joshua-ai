"""The `digest` worker: one digest of a group of near-duplicate journal entries.

The worker has no tools and a timeout of 120 seconds. It reads the entries of
one group in date order, each with its path, its date, and its people, and
answers with the `DigestAnswer` schema: a short `title` and a `summary`.

Python checks the answer. The title goes on one line. The summary loses each
link to a URL that no covered entry holds, and each wikilink and picture. A
summary of more than `MAX_WORDS` words, or an empty one, is a rejection with a
reason. The worker never decides which entries a digest covers: the group
does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import Field

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.sanitize import CleanModel
from joshua_core.engine.usage import Usage
from joshua_core.jobs.digest.groups import Group
from joshua_core.jobs.digest.prompts import DIGEST_PROMPT
from joshua_core.jobs.research.text import allowed_urls, inline, strip_links

WORKER = "digest"
TIMEOUT_S = 120.0
MAX_WORDS = 500
MAX_TITLE = 120
# The most characters of one entry that the worker reads.
MAX_ENTRY_CHARS = 6000

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


class DigestAnswer(CleanModel):
    """The answer schema the worker is asked for."""

    title: str = Field(max_length=400)
    summary: str = Field(max_length=12000)


@dataclass(frozen=True)
class Digest:
    """What one call gave: the title and the summary, or the reason there is
    none."""

    title: str = ""
    summary: str = ""
    reason: str | None = None


def digest_text(group: Group, *, timezone: str, instance: str) -> str:
    """The user text of the `digest` worker for `group`."""
    blocks = []
    for entry in group.entries:
        body = entry.body.strip()
        if len(body) > MAX_ENTRY_CHARS:
            body = body[:MAX_ENTRY_CHARS].rstrip() + "\n[the rest of this entry is cut]"
        people = ", ".join(entry.people) or "none"
        blocks.append(
            f"## {entry.rel}\n\nDate: {entry.day.isoformat()} ({entry.day:%A})\n"
            f"People: {people}\n\n{body}"
        )
    entries = "\n\n".join(blocks)
    return (
        f"Instance: {instance}\n"
        f"Timezone: {timezone}\n"
        f"Entries: {group.size}, from {group.first.isoformat()} to {group.last.isoformat()}\n\n"
        f"<entries>\n{entries}\n</entries>"
    )


def check_answer(answer: DigestAnswer, group: Group) -> Digest:
    """The digest of a valid answer, or the reason it is rejected."""
    title = inline(answer.title, MAX_TITLE).lstrip("# ").strip()
    urls = [u for e in group.entries for u in _URL_RE.findall(e.text)]
    summary = strip_links(answer.summary, allowed_urls(urls)).strip()
    if not title:
        return Digest(reason="the answer has no title")
    if not summary:
        return Digest(reason="the answer has no summary")
    words = len(summary.split())
    if words > MAX_WORDS:
        return Digest(reason=f"the summary has {words} words, more than {MAX_WORDS}")
    return Digest(title=title, summary=summary)


async def write_digest(
    group: Group,
    *,
    user_text: str,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> Digest:
    """Ask the `digest` worker once for `group`."""
    answer = await run_worker(
        name=f"digest-{WORKER}",
        system_prompt=DIGEST_PROMPT,
        user_text=user_text,
        answer=DigestAnswer,
        model=model,
        timeout_s=TIMEOUT_S,
        runner=runner,
        usage_out=usage_out,
    )
    if answer is None:
        return Digest(reason="the worker gave no answer that matches the schema")
    return check_answer(answer, group)


__all__ = [
    "MAX_WORDS",
    "TIMEOUT_S",
    "WORKER",
    "Digest",
    "DigestAnswer",
    "check_answer",
    "digest_text",
    "write_digest",
]

"""The journal view of the viewer: a feed to read, and a form to correct.

The wiki tree shows the journal as ``YYYY/MM/DD`` folders, which is the wrong
shape for reading what happened. This module reads the same files under
``wiki/journal/`` and shows them newest day first, one month at a time, with
the people an entry names and whether the nightly run or the agent wrote it.

The functions here build data and HTML only. The routes, the auth, and the
write live in ``viewer``. A member may edit the text and the people of an
entry; the day, the source, and the file name never change from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from joshua_shared import layout
from joshua_shared.layout import is_hidden
from markdown_it import MarkdownIt

# Raw HTML is disabled, so a ``<script>`` in an entry renders as text.
# The commonmark preset has no tables; the table and strikethrough rules are
# turned on, with no new dependency.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")

# The title of a nightly page that carries no heading of its own.
DAY_PAGE_TITLE = "The day"

# How the frontmatter names the writer of a page.
SOURCE_NIGHTLY = "nightly"
SOURCE_AGENT = "agent"


@dataclass(frozen=True)
class Entry:
    """One journal file: the nightly page for a day, or an entry written that day."""

    day: date
    rel: str  # wiki-relative posix path: journal/YYYY/MM/DD/<name>.md
    slug: str
    title: str
    people: tuple[str, ...]
    source: str
    body: str  # markdown, frontmatter removed, the title heading removed
    front: dict[str, Any]
    is_day_page: bool

    @property
    def wiki_url(self) -> str:
        return "/wiki/" + quote(self.rel)

    @property
    def day_url(self) -> str:
        return day_url(self.day)

    @property
    def edit_url(self) -> str:
        return "/edit/" + quote(self.rel)


# -- files ------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` YAML block from the body.

    Returns ``({}, text)`` when there is no block, when the block does not
    close, or when it is not a mapping. The body keeps its own text as is.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError:
                return {}, text
            body = "\n".join(lines[index + 1 :]).lstrip("\n")
            return (data if isinstance(data, dict) else {}), body
    return {}, text


def render_frontmatter(front: dict[str, Any]) -> str:
    """The ``---`` block for ``front``, in the same shape the gateway tool and
    the nightly run write, so an edit changes only what the person changed."""
    block = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{block}\n---\n\n"


def _people(front: dict[str, Any]) -> tuple[str, ...]:
    """The ids an entry names. ``people`` is a list; a page from before the
    single wiki names one ``person`` instead."""
    raw = front.get("people")
    if raw is None and front.get("person"):
        raw = [front["person"]]
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",")]
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def _first_heading(body: str) -> tuple[str, str]:
    """``(heading, body without that line)`` for the first ``# `` line, or
    ``("", body)`` when the body has none."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = _H1.match(stripped)
        if match:
            rest = "\n".join(lines[:index] + lines[index + 1 :]).lstrip("\n")
            return match.group(1).strip(), rest
        break
    return "", body


def parse_entry(rel: str, day: date, text: str) -> Entry:
    """Build an :class:`Entry` from a file's text. ``rel`` is wiki-relative."""
    front, body = split_frontmatter(text)
    slug = Path(rel).stem
    is_day_page = slug == day.isoformat()
    heading, rest = _first_heading(body)
    if heading:
        title, body = heading, rest
    else:
        title = DAY_PAGE_TITLE if is_day_page else slug.replace("-", " ")
    source = str(front.get("source") or (SOURCE_NIGHTLY if is_day_page else SOURCE_AGENT))
    return Entry(
        day=day,
        rel=rel,
        slug=slug,
        title=title,
        people=_people(front),
        source=source,
        body=body,
        front=front,
        is_day_page=is_day_page,
    )


def load_entries(data_root: Path) -> list[Entry]:
    """Every journal file under a ``YYYY/MM/DD`` folder, newest day first, the
    nightly page first within a day, then the entries by name.

    A file outside a day folder, such as ``journal/legacy/``, is not part of
    the feed. It stays reachable through the wiki. A dot entry is skipped.
    """
    base = layout.journal_root(data_root)
    wiki = layout.wiki_root(data_root)
    if not base.is_dir():
        return []
    entries: list[Entry] = []
    for file in sorted(base.rglob("*.md")):
        if not file.is_file() or is_hidden(file, base):
            continue
        day = layout.journal_day_from_path(file, data_root)
        if day is None:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        entries.append(parse_entry(file.relative_to(wiki).as_posix(), day, text))
    entries.sort(key=lambda e: (-e.day.toordinal(), not e.is_day_page, e.slug))
    return entries


# -- selection --------------------------------------------------------------


def months(entries: list[Entry]) -> list[tuple[int, int]]:
    """The ``(year, month)`` pairs that hold at least one entry, newest first."""
    return sorted({(e.day.year, e.day.month) for e in entries}, reverse=True)


def parse_month(value: str | None) -> tuple[int, int] | None:
    """``YYYY-MM`` to ``(year, month)``, or None for an absent or bad value."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})", value.strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def for_month(entries: list[Entry], month: tuple[int, int]) -> list[Entry]:
    return [e for e in entries if (e.day.year, e.day.month) == month]


def for_person(entries: list[Entry], person_id: str | None) -> list[Entry]:
    if not person_id:
        return entries
    return [e for e in entries if person_id in e.people]


def for_day(entries: list[Entry], day: date) -> list[Entry]:
    return [e for e in entries if e.day == day]


def days(entries: list[Entry]) -> list[date]:
    """The days with an entry, newest first, in the order ``entries`` holds."""
    seen: list[date] = []
    for entry in entries:
        if entry.day not in seen:
            seen.append(entry.day)
    return seen


# -- html -------------------------------------------------------------------


def day_url(day: date) -> str:
    return f"/journal/{day:%Y}/{day:%m}/{day:%d}"


def month_url(month: tuple[int, int], person_id: str | None = None) -> str:
    url = f"/journal?month={month[0]:04d}-{month[1]:02d}"
    if person_id:
        url += f"&person={quote(person_id)}"
    return url


def _month_label(month: tuple[int, int]) -> str:
    return date(month[0], month[1], 1).strftime("%B %Y")


def _day_label(day: date) -> str:
    return day.strftime("%A %-d %B %Y")


def _chips(entry: Entry, names: dict[str, str]) -> str:
    chips = "".join(
        f'<a class=chip href="/journal?person={quote(pid)}">{escape(names.get(pid, pid))}</a>'
        for pid in entry.people
    )
    badge = f'<span class="badge {escape(entry.source)}">{escape(entry.source)}</span>'
    return badge + chips


def entry_html(entry: Entry, names: dict[str, str], *, can_edit: bool) -> str:
    """One entry as an article: title, badge, people, the text, the actions."""
    actions = [f'<a href="{entry.wiki_url}">page</a>']
    if can_edit:
        actions.append(f'<a href="{entry.edit_url}">edit</a>')
    rendered = _MD.render(entry.body) if entry.body.strip() else "<p class=snippet>(empty)</p>"
    return (
        f'<article class="entry" id="{escape(entry.slug, quote=True)}">'
        f'<header><h3><a href="{entry.day_url}#{quote(entry.slug)}">{escape(entry.title)}</a></h3>'
        f"{_chips(entry, names)}</header>"
        f'<div class="md">{rendered}</div>'
        f"<footer>{' · '.join(actions)}</footer>"
        "</article>"
    )


def _day_section(day: date, entries: list[Entry], names: dict[str, str], *, can_edit: bool) -> str:
    articles = "".join(entry_html(e, names, can_edit=can_edit) for e in entries)
    return (
        f'<section class="day"><h2><a href="{day_url(day)}">{escape(_day_label(day))}</a></h2>'
        f"{articles}</section>"
    )


def _month_nav(
    month: tuple[int, int], available: list[tuple[int, int]], person_id: str | None
) -> str:
    """Older and newer month links around the current month, from the months
    that hold an entry. ``available`` is newest first."""
    if month not in available:
        available = sorted(set(available) | {month}, reverse=True)
    index = available.index(month)
    older = available[index + 1] if index + 1 < len(available) else None
    newer = available[index - 1] if index > 0 else None
    parts = []
    if older:
        parts.append(f'<a href="{month_url(older, person_id)}">← {escape(_month_label(older))}</a>')
    parts.append(f"<strong>{escape(_month_label(month))}</strong>")
    if newer:
        parts.append(f'<a href="{month_url(newer, person_id)}">{escape(_month_label(newer))} →</a>')
    return '<p class="months">' + " · ".join(parts) + "</p>"


def _people_filter(
    people: list[tuple[str, str]], person_id: str | None, month: tuple[int, int]
) -> str:
    if not people:
        return ""
    items = [
        '<a class="chip{}" href="{}">everyone</a>'.format(
            "" if person_id else " on", month_url(month)
        )
    ]
    for pid, name in people:
        on = " on" if pid == person_id else ""
        items.append(f'<a class="chip{on}" href="{month_url(month, pid)}">{escape(name)}</a>')
    return '<p class="filters">' + "".join(items) + "</p>"


EMPTY_TEXT = (
    "Nothing in the journal yet. Joshua writes an entry when something is worth "
    "keeping, and a page for the day at the nightly run."
)


def feed_html(
    entries: list[Entry],
    *,
    month: tuple[int, int],
    available: list[tuple[int, int]],
    people: list[tuple[str, str]],
    person_id: str | None,
    can_edit: bool,
) -> str:
    """The feed for one month: the month links, the people filter, then one
    section per day. ``entries`` is the month's slice, already filtered."""
    names = dict(people)
    parts = ["<h1>journal</h1>"]
    parts.append(_month_nav(month, available, person_id))
    parts.append(_people_filter(people, person_id, month))
    if not entries:
        parts.append(f"<p class=snippet>{escape(EMPTY_TEXT)}</p>")
    for day in days(entries):
        parts.append(_day_section(day, for_day(entries, day), names, can_edit=can_edit))
    return "".join(parts)


def day_html(
    day: date,
    entries: list[Entry],
    *,
    older: date | None,
    newer: date | None,
    people: list[tuple[str, str]],
    can_edit: bool,
) -> str:
    """One day: the day links, then the day's entries."""
    names = dict(people)
    nav = []
    if older:
        nav.append(f'<a href="{day_url(older)}">← {escape(_day_label(older))}</a>')
    month = (day.year, day.month)
    nav.append(f'<a href="{month_url(month)}">{escape(_month_label(month))}</a>')
    if newer:
        nav.append(f'<a href="{day_url(newer)}">{escape(_day_label(newer))} →</a>')
    parts = [f"<h1>{escape(_day_label(day))}</h1>", '<p class="months">' + " · ".join(nav) + "</p>"]
    if not entries:
        parts.append("<p class=snippet>Nothing on this day.</p>")
    parts.append("".join(entry_html(e, names, can_edit=can_edit) for e in entries))
    return "".join(parts)


def parse_people_field(value: str) -> tuple[str, ...]:
    """The ids from the form field, each checked as a person id. Raises
    ``ValueError`` for an id that is not one."""
    out: list[str] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        out.append(layout.safe_segment(item))
    return tuple(dict.fromkeys(out))

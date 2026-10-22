"""One insight page, the table of every insight, and expiry.

An insight is at `layout.insight_page(section, slug)`. Its frontmatter is the
`Insight` model. Python writes the body from the frontmatter and the text of
the worker, under fixed headings, in this order: the claim as the title,
**Why it matters**, **What to do**, **What we do**, **What is known**, and
**Evidence**. No worker writes a page.

`insights/README.md` (`layout.insights_index`) is the table of every insight.
`write_index` writes it again on each write of the insights job and of the
`insights` chat tool. The table holds `index: false`, so the memory index
leaves it out; each insight page is indexed.

A person can snooze an open insight: `snoozed_until` is the last day of the
snooze, and `snoozes` counts the snoozes. An insight is *due* when it is open
and its snooze, if any, ended before today (`is_due`). Chat retrieval and the
default list of the `insights` tool show only due insights.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo

from joshua_shared import frontmatter, layout
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_core.jobs.bundle import OPEN_INSIGHT_STATUSES
from joshua_core.jobs.research.text import inline, strip_links
from joshua_core.jobs.topics import ROOT_SECTION as ROOT_SECTION
from joshua_core.jobs.topics import read_page, section_slug

logger = get_logger("jobs.insights.page")

SOURCE = "insights"
EXPIRY = "expiry"

InsightStatus = Literal["new", "told", "acknowledged", "dismissed", "expired"]
Severity = Literal["note", "act", "urgent"]
Confidence = Literal["low", "medium", "high"]
STATUSES: tuple[str, ...] = ("new", "told", "acknowledged", "dismissed", "expired")
OPEN_STATUSES: tuple[str, ...] = OPEN_INSIGHT_STATUSES
SEVERITIES: tuple[str, ...] = ("note", "act", "urgent")
CONFIDENCES: tuple[str, ...] = ("low", "medium", "high")

HEADING_WHY = "Why it matters"
HEADING_ACTION = "What to do"
HEADING_WE_DO = "What we do"
HEADING_KNOWN = "What is known"
HEADING_EVIDENCE = "Evidence"
HEADINGS = (HEADING_WHY, HEADING_ACTION, HEADING_WE_DO, HEADING_KNOWN, HEADING_EVIDENCE)

# The mark of a Markdown heading line, so worker text cannot add a heading.
_HEADING_MARK_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)", re.MULTILINE)


class Insight(BaseModel):
    """The frontmatter of an insight page. A key this model does not know is kept."""

    model_config = ConfigDict(extra="allow")

    topic: str
    section: str
    claim: str
    status: InsightStatus = "new"
    severity: Severity = "note"
    valid_from: date
    valid_until: date
    we_do: list[str] = Field(default_factory=list)
    we_do_paths: list[str] = Field(default_factory=list)
    knowledge_says: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    found_at: date
    seen_at: date
    told_at: date | None = None
    resolved_at: date | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    source: str = SOURCE
    run_id: int | None = None
    snoozed_until: date | None = None
    snoozes: int = Field(default=0, ge=0)

    @property
    def is_open(self) -> bool:
        """True for `new`, `told`, and `acknowledged`."""
        return self.status in OPEN_STATUSES

    def is_snoozed(self, today: date) -> bool:
        """True when the insight is open and its snooze includes `today`."""
        return self.is_open and self.snoozed_until is not None and self.snoozed_until >= today

    def is_due(self, today: date) -> bool:
        """True when the insight is open and not snoozed on `today`."""
        return self.is_open and not self.is_snoozed(today)


@dataclass(frozen=True)
class InsightFile:
    """An insight on the disk: its id (`<section>/<slug>`), its path, its
    frontmatter, and its whole text."""

    id: str
    path: Path
    insight: Insight
    text: str


@dataclass(frozen=True)
class Body:
    """The text of the worker that the body holds. `phrase_paths` maps a
    `we_do` phrase to the bundle path that holds it. `known` maps a
    `knowledge_says` page to its one-line short answer."""

    why: str
    action: str
    evidence: str
    phrase_paths: Mapping[str, str]
    known: Mapping[str, str]


def local_day(now: datetime, settings: JoshuaConfig) -> date:
    """The day of `now` in the timezone of the instance. A naive `now` is
    already local."""
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(ZoneInfo(settings.timezone)).date()


def rank_key(item: InsightFile) -> tuple[int, int, date, str]:
    """The sort key of the ranked list: the highest severity first, then the
    highest confidence, then the oldest `found_at`, then the id."""
    i = item.insight
    return (
        -SEVERITIES.index(i.severity),
        -CONFIDENCES.index(i.confidence),
        i.found_at,
        item.id,
    )


# -- paths ------------------------------------------------------------------


def insight_section(topic_rel: str) -> str:
    """The insight section of a topic: its first path segment as a slug, or
    `general` for a page at the wiki root."""
    return section_slug(topic_rel)


def insight_rel(insight_id: str) -> str:
    """The path of an insight relative to the wiki root."""
    return f"{layout.INSIGHTS}/{insight_id}.md"


def insight_path(insight_id: str, data_root: Path | str) -> Path:
    """The path of `<section>/<slug>`. Raises `ValueError` for any other shape."""
    parts = insight_id.split("/") if isinstance(insight_id, str) else []
    if len(parts) != 2:
        raise ValueError(f"not an insight id: {insight_id!r}")
    return layout.insight_page(parts[0], parts[1], data_root)


def _rel_link(from_file: str, to_file: str) -> str:
    start = PurePosixPath(from_file).parent.as_posix()
    return os.path.relpath(to_file, start if start != "." else os.curdir).replace(os.sep, "/")


def knowledge_rel(rel: str) -> str:
    """The path of a knowledge page relative to the wiki root, from its path
    relative to `wiki/knowledge/`."""
    return f"{layout.KNOWLEDGE}/{rel}"


# -- read ---------------------------------------------------------------------


def parse_insight(text: str) -> Insight | None:
    """The insight in a page text, or None when its frontmatter is not one."""
    meta, _ = frontmatter.split(text)
    if not isinstance(meta.get("topic"), str) or not isinstance(meta.get("claim"), str):
        return None
    try:
        return Insight.model_validate(meta)
    except ValidationError:
        return None


def load_insight(insight_id: str, data_root: Path | str) -> InsightFile | None:
    """One insight by its id, or None when there is none or it does not parse."""
    try:
        path = insight_path(insight_id, data_root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    text = read_page(path)
    insight = parse_insight(text)
    if insight is None:
        return None
    return InsightFile(insight_id, path, insight, text)


def load_insights(data_root: Path | str, topic: str | None = None) -> list[InsightFile]:
    """Every insight under `wiki/insights/<section>/`, sorted by id. With
    `topic`, only the insights whose `topic` is that page."""
    root = layout.insights_root(data_root)
    found: list[InsightFile] = []
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        for page in sorted(folder.glob("*.md")):
            if page.name.startswith(".") or not page.is_file():
                continue
            loaded = load_insight(f"{folder.name}/{page.stem}", data_root)
            if loaded is None:
                continue
            if topic is None or loaded.insight.topic == topic:
                found.append(loaded)
    return found


def body_sections(text: str) -> dict[str, str]:
    """The text under each fixed `## ` heading of an insight page."""
    _, body = frontmatter.split(text)
    out: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## ") and line[3:].strip() in HEADINGS:
            if current is not None:
                out[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        out[current] = "\n".join(lines).strip()
    return out


# -- write --------------------------------------------------------------------


def _plain(text: str) -> str:
    """Worker text with no link and no heading line."""
    return _HEADING_MARK_RE.sub("", strip_links(text.replace("\r\n", "\n"), set())).strip()


def _evidence(text: str, me: str, allowed: Sequence[str]) -> str:
    """Worker evidence with no heading line, and no link except one to a
    wiki path in `allowed`, which gets a target relative to the page."""
    keep = {p.lstrip("/"): p for p in allowed}

    def rewrite(target: str) -> str | None:
        clean = target.split("#", 1)[0].lstrip("/")
        while clean.startswith("../"):
            clean = clean[3:]
        rel = keep.get(clean)
        return _rel_link(me, rel) if rel else None

    text = strip_links(text.replace("\r\n", "\n"), set(), rewrite=rewrite)
    return _HEADING_MARK_RE.sub("", text).strip()


def render_body(insight_id: str, insight: Insight, body: Body) -> str:
    """The body of an insight page."""
    me = insight_rel(insight_id)
    lines = [f"# {inline(_plain(insight.claim), 300)}", ""]
    lines += [f"## {HEADING_WHY}", "", _plain(body.why) or "Not given.", ""]
    lines += [f"## {HEADING_ACTION}", "", _plain(body.action) or "Not given.", ""]
    lines += [f"## {HEADING_WE_DO}", ""]
    for phrase in insight.we_do:
        path = body.phrase_paths.get(phrase) or (
            insight.we_do_paths[0] if insight.we_do_paths else insight.topic
        )
        quote = inline(phrase, 200).replace('"', "'")
        lines.append(f'- "{quote}" in [{path}]({_rel_link(me, path)})')
    lines += ["", f"## {HEADING_KNOWN}", ""]
    for page in insight.knowledge_says:
        target = knowledge_rel(page)
        short = inline(_plain(body.known.get(page, "")), 200)
        lines.append(f"- {short or 'See the page.'} [{target}]({_rel_link(me, target)})")
    allowed = [*insight.we_do_paths, *(knowledge_rel(p) for p in insight.knowledge_says)]
    lines += ["", f"## {HEADING_EVIDENCE}", ""]
    lines.append(_evidence(body.evidence, me, allowed) or "Not given.")
    return "\n".join(lines).rstrip() + "\n"


def _meta(insight: Insight) -> dict[str, Any]:
    return insight.model_dump(mode="python", exclude_none=True)


def render_insight(insight_id: str, insight: Insight, body: Body) -> str:
    """The whole text of an insight page: the frontmatter and the body."""
    return frontmatter.update(render_body(insight_id, insight, body), **_meta(insight))


def write_text(path: Path, text: str, data_root: Path | str) -> Path:
    """Write a file of the insights job. Refuses a path outside `wiki/insights/`."""
    root = layout.insights_root(data_root).resolve()
    target = path.resolve()
    if root not in target.parents:
        raise ValueError(f"not under wiki/insights: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def updated_text(current: InsightFile, **keys: Any) -> tuple[Insight, str]:
    """The insight and the page text with `keys` set in the frontmatter. The
    body stays as it is. Raises `ValueError` for a value the model refuses."""
    try:
        insight = Insight.model_validate({**_meta(current.insight), **keys})
    except ValidationError as exc:
        raise ValueError(f"not a valid insight: {exc.error_count()} errors") from None
    values = {k: v for k, v in _meta(insight).items() if k in keys}
    return insight, frontmatter.update(current.text, **values)


def set_fields(current: InsightFile, data_root: Path | str, **keys: Any) -> InsightFile:
    """Write `keys` into the frontmatter of an insight on the disk."""
    insight, text = updated_text(current, **keys)
    write_text(current.path, text, data_root)
    return InsightFile(current.id, current.path, insight, text)


# -- the index ----------------------------------------------------------------


def _cell(text: str) -> str:
    return inline(text).replace("|", "\\|")


def index_order(insights: Iterable[InsightFile]) -> list[InsightFile]:
    """Open insights first, then the rest; in each group the newest first."""
    items = sorted(insights, key=lambda f: f.id)
    items.sort(key=lambda f: f.insight.found_at, reverse=True)
    items.sort(key=lambda f: 0 if f.insight.is_open else 1)
    return items


def render_index(insights: Iterable[InsightFile]) -> str:
    """The text of `insights/README.md`."""
    me = f"{layout.INSIGHTS}/README.md"
    lines = [
        "# Insights",
        "",
        "Where what we do is different from what is known, now. The insights job "
        "writes this page again on each change. Open insights are first.",
        "",
        "| Status | Severity | Topic | Claim | Valid until | Found |",
        "|---|---|---|---|---|---|",
    ]
    rows = index_order(insights)
    for item in rows:
        i = item.insight
        status = i.status
        if i.is_open and i.snoozed_until is not None:
            status = f"{i.status}, snoozed until {i.snoozed_until.isoformat()}"
        topic = f"[{_cell(i.topic)}]({_rel_link(me, i.topic)})"
        claim = f"[{_cell(_plain(i.claim))}]({_rel_link(me, insight_rel(item.id))})"
        lines.append(
            f"| {status} | {i.severity} | {topic} | {claim} | "
            f"{i.valid_until.isoformat()} | {i.found_at.isoformat()} |"
        )
    if not rows:
        lines.append("| none | | | | | |")
    return frontmatter.update("\n".join(lines) + "\n", source=SOURCE, index=False)


def write_index(data_root: Path | str) -> Path:
    """Write `insights/README.md` from every insight on the disk."""
    text = render_index(load_insights(data_root))
    return write_text(layout.insights_index(data_root), text, data_root)


# -- expiry -------------------------------------------------------------------


def due_to_expire(insights: Iterable[InsightFile], today: date) -> list[InsightFile]:
    """The open insights whose `valid_until` is before `today`."""
    return [f for f in insights if f.insight.is_open and f.insight.valid_until < today]


def expire(
    data_root: Path | str, topic: str, today: date, *, dry_run: bool
) -> tuple[list[InsightFile], list[Path]]:
    """Expire each open insight of `topic` whose `valid_until` is before `today`:
    `status: expired`, `resolved_at: today`, `resolved_by: expiry`. Returns
    the insights that expire and the paths written. A dry run writes nothing,
    and the index is written only when an insight expired."""
    due = due_to_expire(load_insights(data_root, topic), today)
    if dry_run or not due:
        return due, []
    done: list[InsightFile] = []
    written: list[Path] = []
    for item in due:
        changed = set_fields(
            item, data_root, status="expired", resolved_at=today, resolved_by=EXPIRY
        )
        done.append(changed)
        written.append(changed.path)
    written.append(write_index(data_root))
    logger.info({"message": "insights expired", "topic": topic, "count": len(done)})
    return done, written

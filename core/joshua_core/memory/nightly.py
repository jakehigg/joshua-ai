"""Nightly reflection — Joshua writes the day's journal page and each
person's profile.

Every night, for one day of transcripts, the reflector writes one journal
page for the whole instance (``journal_day_page(target)``), from every
person's transcript and the group transcript together, and rewrites a
person's profile (``profile_path(pid)``) when something durable changed. A
final pass rewrites the shared profile (``shared_profile_path()``) from the
day's group chats. The page is the durable memory; the indexer picks it up;
the daily rollover starts the next day's SDK session fresh.

The reflection is one tool-less LLM call per document (``run_oneshot``), so it
never touches a live conversation. One person's profile failing never stops
the others, and never stops the journal page.

Schedule: ``memory.nightly_at`` local time, on the same cron loop as the
scheduler. ``POST /admin/reflect`` runs or re-runs one day on demand; re-running
a date overwrites that day's page.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.cron import compute_next_cron, now_in, zone

logger = get_logger("memory.nightly")

# A reply that wrote a skill file is teaching, not journal material. Its reply is
# dropped from the transcript the reflection reads. Any other write — a journal
# entry, a wiki page — is ordinary work and stays.
_SKILL_DIR = "skills/"

# Shared preference-extraction rules, ported from the old core's PREFERENCE_SYSTEM.
_RULES = """\
Rules:
- Generalize, do not transcribe. Turn a concrete reaction into the standing
  preference behind it. Capture the reason with an aversion.
- One reaction is a hypothesis, not a certainty. Write it as a tendency.
- Never store transient state (a timer, today's plan, a one-off reminder, the
  current weather). Those belong in the journal page, not the profile.
- Ignore feature requests and skill teaching. They become code or skills, not
  memory.
- Most days change nothing durable. Prefer to leave the profile as it is."""

JOURNAL_SYSTEM = """\
You are Joshua, writing today's page in your own journal. The journal is your \
episodic memory: what happened. There is one journal and it is yours. A person \
is named in a page, never the owner of one. Write in the third person, naming \
each person. Return ONE JSON object and nothing else:

{"post": "<markdown> or null}

`post` is the day's page: what happened, what people told you, decisions and \
plans they made, a visit, a change in someone's life. Never invent anything \
that is not in the transcript.

Be selective. Keep only what would still matter in a month, and return \
`"post": null` when nothing today clears that bar. Most days do not.

Leave out:

- Anything that is somebody operating you. A person asking you to do a thing \
is not a life update, however the request is worded. This holds even when the \
tool you were asked to use keeps a record of its own: when a person asks you \
to add an entry to their own journal, blog, or notes somewhere else, the thing \
they wrote is theirs and it lives there. Your journal does not copy it. What \
you may keep is what they told you in the conversation itself.
- A durable trait: what a person is reliably like, what they prefer, what they \
avoid. That belongs in their profile, not here. Keep the event; drop the \
generalization.
- Routine automations, the weather, small talk, and anything already written \
in the wiki.

Two worked examples.

A day worth a page: Alex says a relative arrives on Friday and they are taking \
the week off; Alex asks you to turn a light on; Alex asks you to add today's \
line to a log they keep on another service. The page holds the visit and the \
week off. It holds neither the light nor the line, and the line lives on that \
other service, not here.

A day worth no page: somebody asks the forecast, asks you to set a timer, and \
says good night. Return `"post": null`.

Keep it under 600 words."""

PROFILE_SYSTEM = f"""\
You are Joshua, updating your profile of one person from your conversations \
with them today. Return ONE JSON object and nothing else:

{{"profile": "<full replacement markdown> or null}}

`profile` is a FULL replacement of your profile of them, or null when nothing
durable changed today. Keep every template section heading. Keep it under 400
words.

{_RULES}"""

SHARED_SYSTEM = f"""\
You are Joshua, maintaining the shared profile of the people you serve, from
the group chats today. Return ONE JSON object and nothing else:

{{"profile": "<full replacement markdown> or null}}

`profile` is a FULL replacement of the shared profile (who is who, shared facts,
shared preferences such as "weeknight dinners must be quick"), or null when no
durable change happened today. Keep every template section heading. Keep it
under 400 words. Never invent anything that is not in the transcript.

{_RULES}"""


def _strip_fences(text: str) -> str:
    """Drop a leading ```json fence and a trailing ``` fence, if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_object(raw: str) -> dict[str, Any] | None:
    """Parse the model's reply into a JSON object, or None on any failure."""
    if not raw:
        return None
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _journal_frontmatter(target_date: date, people: list[str]) -> str:
    people_str = ", ".join(people)
    return f"---\ndate: {target_date.isoformat()}\npeople: [{people_str}]\nsource: nightly\n---\n"


class _Bucket:
    """One person's day: rendered lines plus a running character count."""

    def __init__(self) -> None:
        self.rows: list[tuple[datetime, str, str]] = []
        self.chars = 0

    def add(self, when: datetime, label: str, content: str) -> None:
        self.rows.append((when, label, content))
        self.chars += len(content)

    def render(self) -> str:
        lines = [
            f"{label}: {content}" for _, label, content in sorted(self.rows, key=lambda r: r[0])
        ]
        return "\n\n".join(lines)


class NightlyReflector:
    """Write the day's journal page and profiles, then roll sessions over."""

    def __init__(
        self,
        *,
        repo: Any,
        indexer: Any,
        manager: Any,
        settings: JoshuaConfig,
        data_dir: Path | str = "/data",
        oneshot: Any = None,
    ) -> None:
        self._repo = repo
        self._indexer = indexer
        self._manager = manager
        self._settings = settings
        self._tz = settings.timezone
        self._data_dir = Path(data_dir)
        self._min_chars = settings.memory.min_chars_for_post
        self._model = settings.core.reflection_model
        self._daily_rollover = settings.memory.daily_rollover
        self._nightly_at = settings.memory.nightly_at
        self._oneshot = oneshot or _default_oneshot
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # The summary of the last run, for `GET /admin/journal/status`. The
        # object is the one `reflect` returns, so `run_once` adding
        # `rolled_over` after the fact shows up here too.
        self._last_run: dict[str, Any] | None = None

    # --- lifecycle ---------------------------------------------------------

    def _cron(self) -> str:
        """The five-field cron for ``memory.nightly_at`` (``HH:MM`` local)."""
        hour, minute = self._nightly_at.split(":", 1)
        return f"{int(minute)} {int(hour)} * * *"

    async def start(self) -> None:
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="nightly-reflector")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        cron = self._cron()
        while not self._stopped.is_set():
            now = now_in(self._tz)
            try:
                nxt = compute_next_cron(cron, now, self._tz)
            except ValueError as exc:
                logger.error(
                    {"message": "invalid nightly_at; reflector stopped", "error": str(exc)}
                )
                return
            delay = max(1.0, (nxt - now).total_seconds())
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                return  # stop() fired
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — a bad run must not kill the loop
                logger.error({"message": "nightly run failed", "error": str(exc)})

    # --- entry points ------------------------------------------------------

    def _yesterday(self) -> date:
        return now_in(self._tz).date() - timedelta(days=1)

    async def run_once(self) -> dict[str, Any]:
        """The scheduled run: reflect on yesterday, then roll sessions over."""
        summary = await self.reflect(target_date=self._yesterday())
        if self._daily_rollover:
            summary["rolled_over"] = await self._rollover()
        return summary

    async def reflect(
        self, *, target_date: date | None = None, person: str | None = None
    ) -> dict[str, Any]:
        """Write the day's journal page and profiles. No session rollover.

        With ``person`` set, only that person's profile is written, gated on
        their own transcript, and no journal page and no shared pass run. A
        full day run gates the journal page and every profile on the day's
        combined transcript, across every person and the group. Re-running a
        date overwrites that day's page.
        """
        target = target_date or self._yesterday()
        buckets, group = await self._collect(target)
        people = {p.id: p for p in await self._repo.list_people()}
        ids = [person] if person else list(buckets)

        if person is not None:
            combined_chars = buckets[person].chars if person in buckets else 0
        else:
            combined_chars = sum(b.chars for b in buckets.values()) + group.chars

        posts = profiles = errors = reflected = 0
        written: list[Path] = []
        if combined_chars < self._min_chars:
            logger.info(
                {
                    "message": "day too short to reflect",
                    "person": person,
                    "chars": combined_chars,
                    "min_chars": self._min_chars,
                }
            )
        else:
            reflected = len([pid for pid in ids if buckets.get(pid) is not None])
            if person is None:
                try:
                    wrote, ok = await self._reflect_journal(target, ids, buckets, group, people)
                    posts = int(wrote)
                    errors += int(not ok)
                    if wrote:
                        written.append(layout.journal_day_page(target, self._data_dir))
                except Exception as exc:  # noqa: BLE001 — a bad page must not stop the profiles
                    errors += 1
                    logger.error({"message": "journal reflection failed", "error": str(exc)})

            for pid in ids:
                bucket = buckets.get(pid)
                if bucket is None:
                    continue
                try:
                    wrote_profile, ok = await self._reflect_profile(
                        target, people.get(pid), pid, bucket
                    )
                    profiles += int(wrote_profile)
                    errors += int(not ok)
                    if wrote_profile:
                        written.append(layout.profile_path(pid, self._data_dir))
                except Exception as exc:  # noqa: BLE001 — one person must not stop the rest
                    errors += 1
                    logger.error(
                        {"message": "profile reflection failed", "person": pid, "error": str(exc)}
                    )

        shared_updated = False
        if person is None and group.chars >= self._min_chars:
            try:
                shared_updated = await self._reflect_shared(group)
                if shared_updated:
                    written.append(layout.shared_profile_path(self._data_dir))
            except Exception as exc:  # noqa: BLE001 — the shared pass is isolated too
                errors += 1
                logger.error({"message": "shared profile reflection failed", "error": str(exc)})

        self._commit_wiki(target, written)

        summary = {
            "date": target.isoformat(),
            "people": len(ids),
            "people_reflected": reflected,
            "posts_written": posts,
            "profiles_updated": profiles,
            "shared_updated": shared_updated,
            "errors": errors,
        }
        logger.info({"message": "nightly run complete", **summary})
        self._last_run = summary
        return summary

    def journal_status(self) -> dict[str, Any]:
        """What an operator needs to tell that the journal is alive.

        ``last_run`` is the summary of the last reflection this process ran, or
        None when it has run none yet: a restarted core reports None until
        03:30, and the day folder is the ground truth in the meantime.
        """
        today = now_in(self._tz).date()
        day_dir = layout.journal_day_dir(today, self._data_dir)
        page = layout.journal_day_page(today, self._data_dir)
        entries = 0
        if day_dir.is_dir():
            entries = sum(
                1
                for f in day_dir.iterdir()
                if f.is_file() and f.suffix == ".md" and f.name != page.name
            )
        return {
            "date": today.isoformat(),
            "entries_today": entries,
            "day_page_today": page.exists(),
            "nightly_at": self._nightly_at,
            "last_run": self._last_run,
        }

    def _commit_wiki(self, target: date, written: list[Path]) -> None:
        """Commit what this run wrote, then sync anything else that changed
        during the day. Both only when `wiki.git` is enabled."""
        if not wikigit.is_enabled(self._settings):
            return
        wiki_root = layout.wiki_root(self._data_dir)
        if written:
            wikigit.commit(wiki_root, written, f"nightly: {target.isoformat()}")
        wikigit.commit(wiki_root, None, "nightly: sync the wiki")

    # --- journal, profile, and shared passes --------------------------------

    async def _reflect_journal(
        self,
        target: date,
        ids: list[str],
        buckets: dict[str, _Bucket],
        group: _Bucket,
        people: dict[str, Any],
    ) -> tuple[bool, bool]:
        """Write the day's one journal page. Returns ``(wrote, ok)``; ``ok`` is
        False on a parse failure."""
        sections = []
        for pid in ids:
            bucket = buckets.get(pid)
            if bucket is None:
                continue
            name = getattr(people.get(pid), "display_name", None) or pid
            sections.append(f"# {name}'s day\n\n{bucket.render()}")
        if group.chars:
            sections.append(f"# Group chat\n\n{group.render()}")
        user = f"# {target.isoformat()}\n\n" + "\n\n".join(sections)

        data = await self._oneshot_json(JOURNAL_SYSTEM, user, required="post")
        if data is None:
            logger.error(
                {
                    "message": "journal reflection unparseable; skipping the page",
                    "date": target.isoformat(),
                }
            )
            return False, False

        post = data.get("post")
        if not (isinstance(post, str) and post.strip()):
            return False, True

        page_people = sorted(pid for pid in ids if pid in buckets)
        self._write_journal_page(target, page_people, post.strip())
        await self._reindex()
        return True, True

    async def _reflect_profile(
        self, target: date, person: Any, pid: str, bucket: _Bucket
    ) -> tuple[bool, bool]:
        """Update one person's profile. Returns ``(wrote_profile, ok)``; ``ok``
        is False on a parse failure."""
        path = layout.profile_path(pid, self._data_dir)
        existing = _read(path) or "(no profile yet)"
        user = (
            f"# Transcript for {pid} on {target.isoformat()}\n\n{bucket.render()}\n\n"
            f"# Current profile\n\n{existing}"
        )
        data = await self._oneshot_json(PROFILE_SYSTEM, user, required="profile")
        if data is None:
            logger.error(
                {"message": "profile reflection unparseable; skipping person", "person": pid}
            )
            return False, False

        profile = data.get("profile")
        if not (isinstance(profile, str) and profile.strip()):
            return False, True

        _write(path, profile.strip() + "\n")
        await self._reindex()
        return True, True

    async def _reflect_shared(self, group: _Bucket) -> bool:
        path = layout.shared_profile_path(self._data_dir)
        existing = _read(path) or "(no shared profile yet)"
        user = f"# Group chats\n\n{group.render()}\n\n# Current shared profile\n\n{existing}"
        data = await self._oneshot_json(SHARED_SYSTEM, user, required="profile")
        if data is None:
            logger.error({"message": "shared profile reflection unparseable; skipping"})
            return False
        profile = data.get("profile")
        if not (isinstance(profile, str) and profile.strip()):
            return False
        _write(path, profile.strip() + "\n")
        await self._reindex()
        return True

    def _write_journal_page(self, target: date, people: list[str], body: str) -> None:
        path = layout.journal_day_page(target, self._data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_journal_frontmatter(target, people) + body + "\n")

    async def _oneshot_json(
        self, system: str, user: str, *, required: str
    ) -> dict[str, Any] | None:
        """Run the reflection call and parse it. Retry once on a parse failure."""
        for attempt in (1, 2):
            raw = await self._oneshot(system_prompt=system, user_prompt=user, model=self._model)
            data = _parse_object(raw)
            if data is not None and required in data:
                return data
            logger.warning({"message": "reflection parse failed", "attempt": attempt})
        return None

    async def _reindex(self) -> None:
        """Re-index so the new page is searchable by morning. Never fatal.

        Every document the reflector writes is shared scope, so a full-source
        reindex is what makes it findable — there is no per-person partition
        to narrow to.
        """
        try:
            await self._indexer.reindex(source="files")
        except Exception as exc:  # noqa: BLE001 — indexing lag must not fail the run
            logger.warning({"message": "post-reflection reindex failed", "error": str(exc)})

    # --- rollover ----------------------------------------------------------

    async def _rollover(self) -> int:
        """Null every stale SDK session and flush the pool so the next turn is
        fresh. Continuity carries through the profile and the journal."""
        today = now_in(self._tz).date()
        rolled = await self._repo.rollover_sessions(today)
        if rolled:
            await self._manager.flush_sessions()
        logger.info({"message": "daily rollover", "conversations": rolled})
        return rolled

    # --- transcript collection --------------------------------------------

    async def _collect(self, target: date) -> tuple[dict[str, _Bucket], _Bucket]:
        """Group the day's transcript into per-person buckets and a group bucket.

        A per-person conversation goes to its person. A group conversation goes to
        each member speaker (their turns plus Joshua's replies to them) and, whole,
        to the group bucket. A guest's group turns are never attributed to a
        member. Skill-teaching exchanges are dropped.
        """
        z = zone(self._tz)
        start = datetime.combine(target, time.min, tzinfo=z)
        end = start + timedelta(days=1)
        rows = [
            r
            for r in await self._repo.recent_transcripts(start)
            if r.get("created_at") is not None and start <= r["created_at"] < end
        ]

        people = {p.id: p for p in await self._repo.list_people()}
        by_name = {p.display_name: p for p in people.values()}

        by_conv: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for row in rows:
            by_conv.setdefault(row["conversation_id"], []).append(row)

        buckets: dict[str, _Bucket] = {}
        group = _Bucket()

        for cid, conv_rows in by_conv.items():
            info = await self._repo.read_conversation_meta(cid)
            if info is None:
                continue
            skip = _skill_turns(conv_rows)
            if info.get("session_mode") == "shared":
                self._collect_group(conv_rows, skip, by_name, buckets, group)
            else:
                self._collect_direct(conv_rows, skip, info, buckets)
        return buckets, group

    def _collect_direct(
        self,
        conv_rows: list[dict[str, Any]],
        skip: set[str],
        info: dict[str, Any],
        buckets: dict[str, _Bucket],
    ) -> None:
        pid = info.get("person_id")
        if not pid:
            return
        name = info.get("display_name") or pid
        for row in conv_rows:
            content = _usable(row, skip)
            if content is None:
                continue
            label = "Joshua" if row["direction"] == "out" else name
            buckets.setdefault(pid, _Bucket()).add(row["created_at"], label, content)

    def _collect_group(
        self,
        conv_rows: list[dict[str, Any]],
        skip: set[str],
        by_name: dict[str, Any],
        buckets: dict[str, _Bucket],
        group: _Bucket,
    ) -> None:
        # Map each inbound turn to its member speaker, so Joshua's paired reply
        # (same turn_id) lands in the same member's bucket.
        turn_member: dict[str, Any] = {}
        for row in conv_rows:
            if row["direction"] == "out":
                continue
            speaker = row["meta"].get("speaker")
            person = by_name.get(speaker) if speaker else None
            if person is not None and person.role != "guest":
                turn_member[row["meta"].get("turn_id")] = person

        for row in conv_rows:
            content = _usable(row, skip)
            if content is None:
                continue
            if row["direction"] == "out":
                label = "Joshua"
            else:
                label = row["meta"].get("speaker") or "someone"
            group.add(row["created_at"], label, content)
            member = turn_member.get(row["meta"].get("turn_id"))
            if member is not None:
                buckets.setdefault(member.id, _Bucket()).add(row["created_at"], label, content)


def _is_skill_path(path: str) -> bool:
    """True for a skill file: `wiki/skills/x.md`."""
    return _SKILL_DIR in path


def _skill_turns(conv_rows: list[dict[str, Any]]) -> set[str]:
    """Turn ids whose reply wrote only skill files — teaching, dropped from the day.

    A reply that wrote a journal entry or a wiki page is ordinary work and stays.
    A reply that wrote nothing stays. Only a reply whose every write went to a
    skills directory is teaching.
    """
    skipped = set()
    for row in conv_rows:
        if row["direction"] != "out":
            continue
        written = row["meta"].get("written") or []
        if written and all(_is_skill_path(p) for p in written):
            skipped.add(row["meta"].get("turn_id"))
    return skipped


def _usable(row: dict[str, Any], skip: set[str]) -> str | None:
    """The row's content if it belongs in the journal, else None.

    A skill-teaching exchange drops both halves: the instruction that taught the
    skill is configuration, not a life update.
    """
    if row.get("status") == "failed":
        return None
    if row["meta"].get("turn_id") in skip:
        return None
    content = (row.get("content") or "").strip()
    if not content or content == "[IGNORE]":
        return None
    return content


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


async def _default_oneshot(*, system_prompt: str, user_prompt: str, model: str | None) -> str:
    """The real reflection call. Imported lazily so the module loads without the
    SDK in offline tests."""
    from joshua_core.engine.agent import run_oneshot

    return await run_oneshot(system_prompt=system_prompt, user_prompt=user_prompt, model=model)

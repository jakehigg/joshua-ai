"""Nightly reflection — Joshua writes each person's blog post and profile.

Every night, for one day of transcripts, the reflector writes a blog post per
person (``blog/YYYY-MM-DD.md``) and rewrites ``profile.md`` when something durable
changed. A final pass rewrites the shared profile (``shared/profile.md``)
from the day's group chats. The post is the durable memory; the indexer picks it
up; the daily rollover starts the next day's SDK session fresh.

The reflection is one tool-less LLM call per person (``run_oneshot``), so it never
touches a live conversation. One person failing never stops the others.

Schedule: ``memory.nightly_at`` local time, on the same cron loop as the
scheduler. ``POST /admin/reflect`` runs or re-runs one day on demand; re-running a
date overwrites that day's post.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.cron import compute_next_cron, now_in, zone

logger = get_logger("memory.nightly")

# A reply that wrote a skill file is teaching, not journal material. Its reply is
# dropped from the transcript the reflection reads. Any other write — a journal
# post, a wiki page — is ordinary work and stays.
_SKILL_DIR = "skills/"

# Shared preference-extraction rules, ported from the old core's PREFERENCE_SYSTEM.
_RULES = """\
Rules:
- Generalize, do not transcribe. Turn a concrete reaction into the standing
  preference behind it. Capture the reason with an aversion.
- One reaction is a hypothesis, not a certainty. Write it as a tendency.
- Never store transient state (a timer, today's plan, a one-off reminder, the
  current weather). Those belong in the day's post, not the profile.
- Ignore feature requests and skill teaching. They become code or skills, not
  memory.
- Most days change nothing durable. Prefer to leave the profile as it is."""

BLOG_SYSTEM = f"""\
You are Joshua, writing a private daily journal about one person from your \
conversations with them today. Return ONE JSON object and nothing else:

{{"post": "<markdown>", "profile": "<full replacement markdown> or null}}

`post` is the day's entry, in the person's third person, as their assistant:
what happened, what they told you, decisions they made, and things to follow up
on. Never invent anything that is not in the transcript. Omit pleasantries. Keep
it under 600 words. Transient items (a timer, a one-off reminder) belong here,
not in the profile.

`profile` is a FULL replacement of their profile.md, or null when nothing durable
changed today. Keep every template section heading. Keep it under 400 words.

{_RULES}"""

SHARED_SYSTEM = f"""\
You are Joshua, maintaining the shared profile of the people you serve, from
the group chats today. Return ONE JSON object and nothing else:

{{"profile": "<full replacement markdown> or null}}

`profile` is a FULL replacement of shared/profile.md (who is who, shared facts, shared
preferences such as "weeknight dinners must be quick"), or null when nothing
durable changed today. Keep every template section heading. Keep it under 400
words. Never invent anything that is not in the transcript.

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


def _frontmatter(target_date: date, person_id: str) -> str:
    return f"---\ndate: {target_date.isoformat()}\nperson: {person_id}\nsource: nightly\n---\n"


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
    """Write the day's blog posts and profiles, then roll sessions over."""

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
        """Write posts and profiles for one day. No session rollover.

        With ``person`` set, only that person is written and the shared pass is
        skipped. Re-running a date overwrites that day's post.
        """
        target = target_date or self._yesterday()
        buckets, group = await self._collect(target)
        people = {p.id: p for p in await self._repo.list_people()}

        ids = [person] if person else list(buckets)
        posts = profiles = errors = reflected = 0
        for pid in ids:
            bucket = buckets.get(pid)
            if bucket is None:
                logger.info({"message": "no transcript for the day", "person": pid})
                continue
            if bucket.chars < self._min_chars:
                logger.info(
                    {
                        "message": "day too short to reflect",
                        "person": pid,
                        "chars": bucket.chars,
                        "min_chars": self._min_chars,
                    }
                )
                continue
            reflected += 1
            try:
                wrote_post, wrote_profile, ok = await self._reflect_person(
                    target, people.get(pid), pid, bucket
                )
                posts += int(wrote_post)
                profiles += int(wrote_profile)
                errors += int(not ok)
            except Exception as exc:  # noqa: BLE001 — one person must not stop the rest
                errors += 1
                logger.error(
                    {"message": "person reflection failed", "person": pid, "error": str(exc)}
                )

        shared_updated = False
        if person is None and group.chars >= self._min_chars:
            try:
                shared_updated = await self._reflect_shared(group)
            except Exception as exc:  # noqa: BLE001 — the shared pass is isolated too
                errors += 1
                logger.error({"message": "shared profile reflection failed", "error": str(exc)})

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
        return summary

    # --- per-person and shared passes ----------------------------------

    async def _reflect_person(
        self, target: date, person: Any, pid: str, bucket: _Bucket
    ) -> tuple[bool, bool, bool]:
        """Write one person's post and profile. Returns
        ``(wrote_post, wrote_profile, ok)``; ``ok`` is False on a parse failure."""
        profile_path = layout.profile_path(pid, self._data_dir)
        existing = _read(profile_path) or "(no profile yet)"
        user = (
            f"# Transcript for {pid} on {target.isoformat()}\n\n{bucket.render()}\n\n"
            f"# Current profile.md\n\n{existing}"
        )
        data = await self._oneshot_json(BLOG_SYSTEM, user, required="post")
        if data is None:
            logger.error({"message": "reflection unparseable; skipping person", "person": pid})
            return False, False, False

        wrote_post = False
        post = data.get("post")
        if isinstance(post, str) and post.strip():
            self._write_post(target, pid, post.strip())
            wrote_post = True

        wrote_profile = False
        profile = data.get("profile")
        if isinstance(profile, str) and profile.strip():
            _write(profile_path, profile.strip() + "\n")
            wrote_profile = True

        if wrote_post or wrote_profile:
            await self._reindex(person=pid)
        return wrote_post, wrote_profile, True

    async def _reflect_shared(self, group: _Bucket) -> bool:
        path = layout.shared_profile_path(self._data_dir)
        existing = _read(path) or "(no shared profile yet)"
        user = f"# Group chats\n\n{group.render()}\n\n# Current shared/profile.md\n\n{existing}"
        data = await self._oneshot_json(SHARED_SYSTEM, user, required="profile")
        if data is None:
            logger.error({"message": "shared profile reflection unparseable; skipping"})
            return False
        profile = data.get("profile")
        if not (isinstance(profile, str) and profile.strip()):
            return False
        _write(path, profile.strip() + "\n")
        await self._reindex(person=None)
        return True

    def _write_post(self, target: date, pid: str, body: str) -> None:
        blog = layout.person_dir(pid, "blog", self._data_dir)
        blog.mkdir(parents=True, exist_ok=True)
        (blog / f"{target.isoformat()}.md").write_text(_frontmatter(target, pid) + body + "\n")

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

    async def _reindex(self, *, person: str | None) -> None:
        """Re-index so the new files are searchable by morning. Never fatal."""
        try:
            await self._indexer.reindex(source="files", person=person)
        except Exception as exc:  # noqa: BLE001 — indexing lag must not fail the run
            logger.warning({"message": "post-reflection reindex failed", "error": str(exc)})

    # --- rollover ----------------------------------------------------------

    async def _rollover(self) -> int:
        """Null every stale SDK session and flush the pool so the next turn is
        fresh. Continuity carries through the profile and recent posts."""
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

    A reply that wrote a journal post or a wiki page is ordinary work and stays.
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
    """The row's content if it belongs in a journal, else None.

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

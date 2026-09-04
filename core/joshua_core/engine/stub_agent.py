"""Stub agent backend for offline testing (``AGENT_BACKEND=stub``).

It matches the connect/run/interrupt/close surface of ``AgentSession`` but never
imports the SDK or contacts a model. It streams a deterministic reply so the
whole service — DB, routing, scheduler — runs without the SDK, a Node CLI, or a
network. Real behavior comes from ``AGENT_BACKEND=sdk``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from joshua_shared import layout, wikigit
from joshua_shared.log import get_logger

from joshua_core.engine.types import OnDelta, TurnResult

logger = get_logger("engine.stub")

# A prompt that contains this marker makes the stub echo the MCP server names it
# was built with, so a test can assert which servers a session would open.
ECHO_TOOLS_MARKER = "[[echo-tools]]"

# A prompt that contains this marker makes the stub write a journal entry
# naming the turn speaker, standing in for the SDK agent's
# ``write_journal_entry`` call, so the on-demand journal flow runs end to end
# without the SDK.
JOURNAL_MARKER = "[[journal]]"

# The files-MCP-relative attachment paths cited in a turn's attach note.
_ATTACHMENT_RE = re.compile(r"attachments/[^\s)]+")


class StubAgentSession:
    def __init__(
        self,
        system_prompt: str,
        cwd: Path,
        conversation_id: str,
        *,
        server_names: list[str] | None = None,
        resume: str | None = None,
        data_dir: Path | str | None = None,
        wiki_git: bool = True,
    ):
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._session_id = f"stub-{conversation_id}"
        self._server_names = sorted(server_names or [])
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._wiki_git = wiki_git
        # When built with a resume id, fail the first run once so the manager's
        # resume-retry path (rebuild without resume) is exercised offline.
        self._resume = resume
        self._resume_failed = False
        self._connected = False
        # The turn speaker and journal preference, rebound per turn by the manager.
        self._turn_person: str | None = None
        self._journal_mode = "auto"
        # How many journal posts this session has written; a test asserts on it.
        self.journal_writes = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def set_turn_context(self, person_id: str | None, journal_mode: str = "auto") -> None:
        """Bind the current turn's speaker and journal preference before ``run``."""
        self._turn_person = person_id
        self._journal_mode = journal_mode

    async def connect(self) -> None:
        self._connected = True

    async def run(self, prompt: str, on_delta: OnDelta | None = None) -> TurnResult:
        if not self._connected:
            await self.connect()

        if self._resume and not self._resume_failed:
            self._resume_failed = True
            raise RuntimeError(f"stub resume failed for session {self._resume}")

        if JOURNAL_MARKER in prompt:
            self._write_journal(prompt)

        if ECHO_TOOLS_MARKER in prompt:
            reply = "tools: " + ", ".join(self._server_names)
        else:
            snippet = " ".join(prompt.split())[:120]
            reply = f"(stub) Got it — you said: {snippet}"

        # Stream in a few chunks so the delta path is exercised.
        words = reply.split(" ")
        for i in range(0, len(words), 4):
            chunk = " ".join(words[i : i + 4])
            if i + 4 < len(words):
                chunk += " "
            if on_delta is not None:
                await on_delta(chunk)
            await asyncio.sleep(0)

        return TurnResult(
            text=reply,
            session_id=self._session_id,
            is_error=False,
            usage={"input_tokens": 0, "output_tokens": 0},
            cost_usd=0.0,
        )

    def _write_journal(self, prompt: str) -> None:
        """Write one journal entry naming the turn speaker, under today's folder.

        Mirrors what the SDK agent does through ``write_journal_entry``: place
        the entry under the day's journal folder, name the speaker in
        ``people``, and cite the turn's attachments in the body. Writes
        nothing when there is no resolved speaker (a group turn with no
        sender) or when the speaker turned journaling off.
        """
        if self._turn_person is None or self._data_dir is None:
            return
        if self._journal_mode == "off":
            return

        attachments = _unique(_ATTACHMENT_RE.findall(prompt))
        message = prompt.split("\n\n")[-1].replace(JOURNAL_MARKER, "").strip()

        now = datetime.now().astimezone()
        slug = f"{now:%H%M}-journal"
        entry = layout.journal_entry_path(now.date(), slug, self._data_dir)
        entry.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "---",
            f"date: {now.date().isoformat()}",
            f"people: [{self._turn_person}]",
            "source: agent",
        ]
        if attachments:
            lines.append(f"attachments: [{', '.join(attachments)}]")
        lines += ["---", "", message or "Journal note."]
        if attachments:
            lines.append("")
            lines.append(f"Photo: {attachments[0]}")
        entry.write_text("\n".join(lines) + "\n")
        self.journal_writes += 1
        if self._wiki_git:
            wiki_root = layout.wiki_root(self._data_dir)
            rel = entry.relative_to(wiki_root).as_posix()
            wikigit.commit(wiki_root, [entry], f"journal: {rel}")

    async def interrupt(self) -> None:
        logger.info({"message": "stub interrupt"})

    async def close(self) -> None:
        self._connected = False


def _unique(items: list[str]) -> list[str]:
    """De-duplicate a list, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out

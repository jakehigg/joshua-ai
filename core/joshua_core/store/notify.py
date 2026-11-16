"""The ``notify_prefs`` rows: when Joshua may reach one person of one workspace.

A row says what ``notify_user`` may do for one person in one workspace
(``engine/tools/workspace.py``):

- ``off`` — never notify them there;
- ``decide`` — notify them when the thread needs them, by the rules of
  ``prompts/builtin/workspace_shared.md``;
- ``all`` — notify them on every turn another person writes.

A person with no row takes the default of the workspace: ``decide`` in a
shared one, ``off`` in a personal one (``default_mode``). So the table is empty
until somebody changes a mode, and nothing reads it before that.

The row also holds ``last_notified_at``, which the ten-minute guard of the
tool reads: one notification per person per workspace in that time, and a line
inside it folds into the notification already sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.store.db import Database

logger = get_logger("store.notify")

# The three modes, as the table's CHECK holds them.
OFF = "off"
DECIDE = "decide"
ALL = "all"
NOTIFY_MODES = (OFF, DECIDE, ALL)


def default_mode(shared: bool) -> str:
    """The mode of a person with no row: ``decide`` in a shared workspace, and
    ``off`` in a personal one, where nobody else writes."""
    return DECIDE if shared else OFF


@dataclass(frozen=True)
class NotifyPref:
    """One row of ``notify_prefs``."""

    person_id: str
    workspace_id: str
    mode: str = DECIDE
    # When this person was last notified of this workspace, or None.
    last_notified_at: datetime | None = None
    updated_at: datetime | None = None


def _pref(row: dict[str, Any]) -> NotifyPref:
    return NotifyPref(
        person_id=row["person_id"],
        workspace_id=str(row["workspace_id"]),
        mode=row["mode"],
        last_notified_at=row.get("last_notified_at"),
        updated_at=row.get("updated_at"),
    )


class NotifyStore:
    """The ``notify_prefs`` table: read one, list a workspace's, set a mode,
    and stamp a send.

    ``Repo`` holds one of these as ``repo.notify_prefs``.
    """

    def __init__(self, db: Database):
        self._db = db

    @property
    def _pool(self):  # type: ignore[no-untyped-def]
        assert self._db.pool is not None, "Database not connected"
        return self._db.pool

    async def get(self, person_id: str, workspace_id: str) -> NotifyPref | None:
        """The row of this person and this workspace, or None when they have
        none and the default stands."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM notify_prefs WHERE person_id = %s AND workspace_id = %s",
                (person_id, workspace_id),
            )
            row = await cur.fetchone()
        return _pref(row) if row else None

    async def list_for(self, workspace_id: str) -> list[NotifyPref]:
        """Every row of one workspace. A person with no row is not in the
        answer: they take ``default_mode``."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM notify_prefs WHERE workspace_id = %s ORDER BY person_id",
                (workspace_id,),
            )
            rows = await cur.fetchall()
        return [_pref(row) for row in rows]

    async def set_mode(self, person_id: str, workspace_id: str, mode: str) -> NotifyPref:
        """Write the mode of one person in one workspace, and return the row.

        An unknown mode raises ``ValueError``: the route and the control offer
        the three and nothing else.
        """
        if mode not in NOTIFY_MODES:
            raise ValueError(f"mode must be one of: {', '.join(NOTIFY_MODES)}")
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO notify_prefs (person_id, workspace_id, mode)
                VALUES (%s, %s, %s)
                ON CONFLICT (person_id, workspace_id) DO UPDATE
                SET mode = EXCLUDED.mode, updated_at = now()
                RETURNING *
                """,
                (person_id, workspace_id, mode),
            )
            row = await cur.fetchone()
        assert row is not None  # RETURNING of an upsert always gives the row
        logger.info({"message": "notify mode set", "person": person_id, "mode": mode})
        return _pref(row)

    async def mark_sent(
        self, person_id: str, workspace_id: str, *, mode: str = DECIDE, at: datetime | None = None
    ) -> NotifyPref:
        """Stamp the moment this person was notified of this workspace.

        A person with no row gets one, with ``mode``: the mode that the send
        ran under, so the row says what it already was. ``at`` is the moment,
        and None is now.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO notify_prefs (person_id, workspace_id, mode, last_notified_at)
                VALUES (%s, %s, %s, COALESCE(%s, now()))
                ON CONFLICT (person_id, workspace_id) DO UPDATE
                SET last_notified_at = COALESCE(EXCLUDED.last_notified_at, now())
                RETURNING *
                """,
                (person_id, workspace_id, mode if mode in NOTIFY_MODES else DECIDE, at),
            )
            row = await cur.fetchone()
        assert row is not None
        return _pref(row)

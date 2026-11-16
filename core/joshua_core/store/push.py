"""The ``push_subscriptions`` rows: one per browser a person subscribed with.

A browser that turns notifications on sends its subscription to the viewer,
which passes it to core (``joshua_core.push_api``). One person has several
rows, one per device. The endpoint is the URL of that browser at its push
service, so it is the identity of a subscription: a browser that subscribes
again updates the row it already has.

``p256dh`` and ``auth`` are the keys of the browser, as base64url text, the
way the browser sends them. The sender encrypts each payload to them
(``joshua_core.push``), and nothing else reads them.

``last_ok_at`` and ``failures`` are what the sender writes back. A push
service that answers 404 or 410 means the browser is gone, so the row goes at
once; any other refusal raises ``failures``, and a row that reaches
``FAILURE_LIMIT`` is dropped. Nothing here retries, and no method raises into
a turn: the sender catches what it must.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.store.db import Database

logger = get_logger("store.push")

# A subscription that the push service refuses this many times in a row is
# dropped: the browser is gone, or the service will not take it again.
FAILURE_LIMIT = 5

# The longest text each field of a subscription may hold.
ENDPOINT_MAX = 2000
KEY_MAX = 256
USER_AGENT_MAX = 300


@dataclass(frozen=True)
class PushSubscription:
    """One row of ``push_subscriptions``."""

    id: str
    person_id: str
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str = ""
    created_at: datetime | None = None
    last_ok_at: datetime | None = None
    failures: int = 0


def _subscription(row: dict[str, Any]) -> PushSubscription:
    return PushSubscription(
        id=str(row["id"]),
        person_id=row["person_id"],
        endpoint=row["endpoint"],
        p256dh=row["p256dh"],
        auth=row["auth"],
        user_agent=row["user_agent"] or "",
        created_at=row.get("created_at"),
        last_ok_at=row.get("last_ok_at"),
        failures=int(row["failures"]),
    )


class PushStore:
    """The ``push_subscriptions`` table: store one, list a person's, drop one.

    ``Repo`` holds one of these as ``repo.push_subscriptions``.
    """

    def __init__(self, db: Database):
        self._db = db

    @property
    def _pool(self):  # type: ignore[no-untyped-def]
        assert self._db.pool is not None, "Database not connected"
        return self._db.pool

    async def upsert(
        self,
        person_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        *,
        user_agent: str = "",
    ) -> PushSubscription:
        """Store the subscription of one browser, or update the one it has.

        The endpoint is unique, so a browser that subscribes again moves to
        this person, takes its new keys, and starts from no failure.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO push_subscriptions (person_id, endpoint, p256dh, auth, user_agent)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (endpoint) DO UPDATE SET
                    person_id = EXCLUDED.person_id,
                    p256dh = EXCLUDED.p256dh,
                    auth = EXCLUDED.auth,
                    user_agent = EXCLUDED.user_agent,
                    failures = 0
                RETURNING *
                """,
                (person_id, endpoint[:ENDPOINT_MAX], p256dh, auth, user_agent[:USER_AGENT_MAX]),
            )
            row = await cur.fetchone()
        assert row is not None  # RETURNING of an upsert always gives the row
        logger.info({"message": "push subscription stored", "person": person_id})
        return _subscription(row)

    async def list_for(self, person_id: str) -> list[PushSubscription]:
        """Every subscription of one person, oldest first."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM push_subscriptions WHERE person_id = %s ORDER BY created_at",
                (person_id,),
            )
            rows = await cur.fetchall()
        return [_subscription(row) for row in rows]

    async def delete(self, endpoint: str, *, person_id: str | None = None) -> bool:
        """Drop one subscription by endpoint. False when no row held it.

        With ``person_id`` the delete is that person's alone, so one person
        can never remove the subscription of another. The sender passes none:
        a push service that says the endpoint is gone speaks for the row.
        """
        sql = "DELETE FROM push_subscriptions WHERE endpoint = %s"
        params: tuple[Any, ...] = (endpoint,)
        if person_id is not None:
            sql += " AND person_id = %s"
            params = (endpoint, person_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
        return cur.rowcount > 0

    async def mark_ok(self, endpoint: str) -> None:
        """Note that the push service took a notification for this endpoint."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE push_subscriptions SET last_ok_at = now(), failures = 0 "
                "WHERE endpoint = %s",
                (endpoint,),
            )

    async def mark_failure(self, endpoint: str) -> bool:
        """Raise the failure count of one endpoint, and drop a row over the limit.

        True when the row is gone: either it reached ``FAILURE_LIMIT`` or it
        was not there any more.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE push_subscriptions SET failures = failures + 1 "
                "WHERE endpoint = %s RETURNING failures",
                (endpoint,),
            )
            row = await cur.fetchone()
        if row is None:
            return True
        if int(row["failures"]) < FAILURE_LIMIT:
            return False
        await self.delete(endpoint)
        logger.info({"message": "push subscription dropped", "failures": row["failures"]})
        return True

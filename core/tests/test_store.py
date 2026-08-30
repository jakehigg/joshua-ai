"""Integration tests for the store: schema, seeding, and the repo layer.

These run against a live Postgres (``DATABASE_URL``); the CI ``test:core`` job
starts a pgvector service for them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_core.store.db import Database
from joshua_core.store.seed import seed_people
from joshua_shared import config as config_module

pytestmark = pytest.mark.integration

CONFIG_YAML = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
    handles:
      telegram: "998877"
      imessage: "+15551234567"
  - id: mia
    name: Mia
    role: guest
    handles:
      telegram: "112233"
"""


async def test_schema_applies_twice(db: Database) -> None:
    # The `db` fixture already applied the schema once; a second apply is a no-op.
    await db.init_schema()
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM people")
        row = await cur.fetchone()
    assert row["n"] == 0


async def test_seed_people_and_handles(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)

    people = await repo.list_people()
    assert {p.id for p in people} == {"alex", "mia"}
    assert (await repo.get_person("mia")).role == "guest"

    assert dict(await repo.list_person_handles("telegram")) == {
        "998877": "alex",
        "112233": "mia",
    }
    assert (await repo.get_person_by_handle("imessage", "+15551234567")).id == "alex"


async def test_handles_for_person(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)

    assert await repo.handles_for_person("alex") == [
        ("imessage", "+15551234567"),
        ("telegram", "998877"),
    ]
    assert await repo.handles_for_person("nobody") == []


async def test_remove_person_marks_removed_and_drops_handles(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)

    assert await repo.remove_person("alex") is True
    assert (await repo.get_person("alex")).role == "removed"
    assert await repo.handles_for_person("alex") == []
    assert await repo.get_person_by_handle("telegram", "998877") is None
    # A missing person is a no-op.
    assert await repo.remove_person("nobody") is False


async def test_reseed_updates_name_and_keeps_absent_person(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)

    # A person only in the DB survives a reseed.
    await repo.upsert_person("ghost", "Ghost", "member")

    renamed = CONFIG_YAML.replace("name: Alex", "name: Jacob")
    await seed_people(repo, config_module.parse(renamed))

    assert (await repo.get_person("alex")).display_name == "Jacob"
    assert (await repo.get_person("ghost")) is not None


async def test_conversation_uniqueness(repo) -> None:
    await repo.upsert_channel("telegram:1", "telegram", session_mode="per_person")

    # Person is required by the FK; seed one.
    await repo.upsert_person("alex", "Alex")

    a = await repo.get_or_create_conversation("telegram:1", "alex")
    b = await repo.get_or_create_conversation("telegram:1", "alex")
    assert a.id == b.id  # one row per (channel, person)

    anon1 = await repo.get_or_create_conversation("telegram:1", None)
    anon2 = await repo.get_or_create_conversation("telegram:1", None)
    assert anon1.id == anon2.id  # one anon row per channel
    assert anon1.id != a.id


async def test_resolve_channel_forms(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("telegram:group1", "telegram", session_mode="shared")
    await repo.upsert_channel(
        "telegram:dmalex", "telegram", session_mode="per_person", default_person_id="alex"
    )

    assert (await repo.resolve_channel("telegram:group1")).id == "telegram:group1"  # literal
    assert (await repo.resolve_channel("telegram:group")).id == "telegram:group1"  # <type>:group
    assert (await repo.resolve_channel("telegram:dm:alex")).id == "telegram:dmalex"
    assert await repo.resolve_channel("telegram:dm:nobody") is None


async def test_set_session_and_touch(repo) -> None:
    await repo.upsert_channel("voice:office", "voice")
    conv = await repo.get_or_create_conversation("voice:office", None)

    await repo.set_sdk_session(conv.id, "sess-1", last_channel_id="voice:office")
    reread = await repo.get_conversation(conv.id)
    assert reread.sdk_session_id == "sess-1"
    assert reread.last_channel_id == "voice:office"

    await repo.touch_conversation(conv.id, last_channel_id="voice:kitchen")
    assert (await repo.get_conversation(conv.id)).last_channel_id == "voice:kitchen"


async def test_task_lifecycle(repo) -> None:
    await repo.upsert_channel("voice:office", "voice")
    conv = await repo.get_or_create_conversation("voice:office", None)
    now = datetime.now(UTC)

    task = await repo.create_task(
        conv.id, "wake me", now - timedelta(minutes=1), cron_expr="* * * * *"
    )
    assert (await repo.get_task(task.id)).prompt == "wake me"
    assert [t.id for t in await repo.list_tasks(conv.id)] == [task.id]

    claimed = await repo.claim_due_tasks(now)
    assert [t.id for t in claimed] == [task.id]
    # A second claim finds nothing (the row is now 'processing').
    assert await repo.claim_due_tasks(now) == []

    assert await repo.reschedule_task(task.id, now + timedelta(minutes=1)) is True
    assert (await repo.get_task(task.id)).status == "active"

    assert await repo.update_task(task.id, prompt="wake me gently", conversation_id=conv.id) is True
    assert (await repo.get_task(task.id)).prompt == "wake me gently"

    assert await repo.set_task_status(task.id, "paused", conversation_id=conv.id) is True
    # A different (valid) conversation does not own the task, so the guard blocks it.
    await repo.upsert_channel("voice:kitchen", "voice")
    other_conv = await repo.get_or_create_conversation("voice:kitchen", None)
    assert await repo.set_task_status(task.id, "active", conversation_id=other_conv.id) is False

    await repo.complete_task(task.id)
    assert (await repo.get_task(task.id)).status == "completed"

    await repo.fail_task(task.id, "boom", reactivate=False)
    assert (await repo.get_task(task.id)).last_error == "boom"

    # reset_processing re-queues stuck rows.
    await repo.upsert_channel("voice:office", "voice")
    other = await repo.create_task(conv.id, "again", now)
    await repo.claim_due_tasks(now)
    assert await repo.reset_processing() >= 1
    assert (await repo.get_task(other.id)).status == "active"


async def test_transcript_and_kb_event(repo) -> None:
    await repo.upsert_channel("voice:office", "voice")
    conv = await repo.get_or_create_conversation("voice:office", None)

    await repo.add_transcript(conv.id, "in", "hello", meta={"turn_id": "t1"})
    await repo.add_transcript(conv.id, "out", "hi there", meta={"turn_id": "t1"})

    tail = await repo.transcript_tail(conv.id)
    assert [r["direction"] for r in tail] == ["in", "out"]

    recent = await repo.recent_transcripts(datetime(2000, 1, 1, tzinfo=UTC))
    assert len(recent) == 2

    meta = await repo.read_conversation_meta(conv.id)
    assert meta["channel_type"] == "voice"
    assert await repo.latest_conversation_for("nobody") is None

    # kb_event is write-only; a successful insert is the assertion.
    await repo.add_kb_event(kind="search", query="weather", conversation_id=conv.id, turn_id="t1")


async def test_latest_conversation_for_member(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("telegram:dmalex", "telegram", default_person_id="alex")
    conv = await repo.get_or_create_conversation("telegram:dmalex", "alex")
    await repo.touch_conversation(conv.id)

    assert await repo.latest_conversation_for("alex") == conv.id
    assert await repo.latest_conversation_for("alex", channel_like="telegram") == conv.id

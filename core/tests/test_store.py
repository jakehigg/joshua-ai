"""Integration tests for the store: schema, seeding, and the repo layer.

These run against a live Postgres (``DATABASE_URL``); the CI ``test:core`` job
starts a pgvector service for them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from joshua_core.store.db import Database
from joshua_core.store.repo import AUTH_IMPORT_MARKER_ID
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


async def _auth_updated_at(repo, person_id: str) -> datetime:
    """The ``updated_at`` of a person's secret. No repo method gives it, and no
    route may, so the test reads the column itself."""
    async with repo._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT updated_at FROM auth_secret WHERE person_id = %s", (person_id,)
        )
        return (await cur.fetchone())["updated_at"]


async def test_schema_applies_twice(db: Database) -> None:
    # The `db` fixture already applied the schema once; a second apply is a no-op.
    await db.init_schema()
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM people")
        row = await cur.fetchone()
    assert row["n"] == 0


async def test_a_workspace_row_is_personal_and_a_shared_slug_is_one_for_everyone(repo) -> None:
    """The columns of a shared workspace are additive: a row written without
    them is personal, and only a shared slug is unique for every owner."""
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_person("mia", "Mia")
    insert = (
        "INSERT INTO workspaces (slug, title, owner, shared, members) "
        "VALUES (%s, 'Trip', %s, %s, %s::jsonb)"
    )
    async with repo._pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO workspaces (slug, title, owner) VALUES ('trip', 'Trip', 'alex') "
            "RETURNING shared, members"
        )
        assert await cur.fetchone() == {"shared": False, "members": []}
        # The same personal slug for another owner is free.
        await conn.execute(insert, ("trip", "mia", False, "[]"))
        await conn.execute(insert, ("shared_trip", "alex", True, '["mia"]'))
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(insert, ("shared_trip", "mia", True, '["alex"]'))


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


async def test_auth_secret_crud(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)

    assert await repo.get_auth_secret("alex") is None
    assert await repo.list_auth_person_ids() == []
    # A person with no secret is not listed, and deleting nothing is False.
    assert await repo.delete_auth_secret("alex") is False

    await repo.set_auth_secret("alex", "$2b$04$hash-one")
    await repo.set_auth_secret("mia", "$2b$04$hash-mia")
    assert await repo.get_auth_secret("alex") == "$2b$04$hash-one"
    assert await repo.list_auth_person_ids() == ["alex", "mia"]

    # The upsert replaces the hash and bumps updated_at.
    first = await _auth_updated_at(repo, "alex")
    await repo.set_auth_secret("alex", "$2b$04$hash-two")
    assert await repo.get_auth_secret("alex") == "$2b$04$hash-two"
    assert await _auth_updated_at(repo, "alex") >= first

    assert await repo.delete_auth_secret("alex") is True
    assert await repo.get_auth_secret("alex") is None
    assert await repo.list_auth_person_ids() == ["mia"]


async def test_auth_secret_cascades_on_a_hard_delete(repo) -> None:
    cfg = config_module.parse(CONFIG_YAML)
    await seed_people(repo, cfg)
    await repo.set_auth_secret("alex", "$2b$04$hash-one")
    await repo.set_auth_secret("mia", "$2b$04$hash-mia")

    # remove_person keeps the row, so the secret stays.
    await repo.remove_person("alex")
    assert await repo.get_auth_secret("alex") == "$2b$04$hash-one"

    # A hard delete of the person takes the secret with it.
    async with repo._pool.connection() as conn:
        await conn.execute("DELETE FROM people WHERE id = 'alex'")
    assert await repo.get_auth_secret("alex") is None
    assert await repo.list_auth_person_ids() == ["mia"]


async def test_auth_import_marker_set_and_get(repo) -> None:
    assert await repo.get_auth_import_marker() is None

    await repo.set_auth_import_marker(3)
    marker = await repo.get_auth_import_marker()
    assert marker["id"] == AUTH_IMPORT_MARKER_ID
    assert marker["rows_written"] == 3
    assert marker["done_at"] is not None

    # Setting it again replaces the one row.
    await repo.set_auth_import_marker(5)
    assert (await repo.get_auth_import_marker())["rows_written"] == 5
    async with repo._pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM auth_import_marker")
        assert (await cur.fetchone())["n"] == 1


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
    assert reread.inference_endpoint is None

    await repo.set_sdk_session(conv.id, "sess-2", inference_endpoint="litellm:4000")
    reread = await repo.get_conversation(conv.id)
    assert (reread.sdk_session_id, reread.inference_endpoint) == ("sess-2", "litellm:4000")

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


async def test_find_conversation_never_creates_one(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("cli:alex", "cli")
    assert await repo.find_conversation("cli:alex", "alex") is None
    assert await repo.find_conversation("cli:alex", "alex") is None
    created = await repo.get_or_create_conversation("cli:alex", "alex")
    found = await repo.find_conversation("cli:alex", "alex")
    assert found is not None and found.id == created.id
    assert await repo.find_conversation("cli:alex", None) is None


async def test_transcript_page_pages_back_oldest_first(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("cli:alex", "cli")
    conv = await repo.get_or_create_conversation("cli:alex", "alex")
    other = await repo.get_or_create_conversation("cli:alex", None)
    for i in range(5):
        await repo.add_transcript(conv.id, "in" if i % 2 == 0 else "out", f"m{i}")
    await repo.add_transcript(other.id, "in", "not this conversation")

    page, cursor = await repo.transcript_page(conv.id, limit=2)
    assert [r["content"] for r in page] == ["m3", "m4"]
    assert cursor == page[0]["id"]
    assert page[0]["id"] < page[1]["id"]
    assert page[0]["created_at"] is not None

    page, cursor = await repo.transcript_page(conv.id, limit=2, before=cursor)
    assert [r["content"] for r in page] == ["m1", "m2"]

    page, cursor = await repo.transcript_page(conv.id, limit=2, before=cursor)
    assert [r["content"] for r in page] == ["m0"]
    assert cursor is None


async def test_transcript_page_with_exactly_limit_rows_has_no_cursor(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("cli:alex", "cli")
    conv = await repo.get_or_create_conversation("cli:alex", "alex")
    for i in range(3):
        await repo.add_transcript(conv.id, "in", f"m{i}")
    page, cursor = await repo.transcript_page(conv.id, limit=3)
    assert len(page) == 3
    assert cursor is None


async def test_transcript_page_clamps_the_limit(repo) -> None:
    from joshua_core.store.repo import TRANSCRIPT_PAGE_MAX

    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("cli:alex", "cli")
    conv = await repo.get_or_create_conversation("cli:alex", "alex")
    for i in range(3):
        await repo.add_transcript(conv.id, "in", f"m{i}")
    page, cursor = await repo.transcript_page(conv.id, limit=0)
    assert [r["content"] for r in page] == ["m2"]
    assert cursor == page[0]["id"]
    page, _ = await repo.transcript_page(conv.id, limit=TRANSCRIPT_PAGE_MAX + 50)
    assert len(page) == 3


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


async def test_transcript_page_leaves_out_a_silent_reply(repo) -> None:
    """A workspace thread (or any thread) never lists an `[IGNORE]` reply.

    Delivery drops the marker, so the viewer must not show it. The row stays
    stored; a failed reply and an event line are still listed.
    """
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("viewer:alex:workspace:w1", "viewer")
    conv = await repo.get_or_create_conversation("viewer:alex:workspace:w1", "alex")
    await repo.add_transcript(conv.id, "in", "hello")
    await repo.add_transcript(conv.id, "inject", "Developer task d335c034 has a question")
    await repo.add_transcript(conv.id, "out", "[IGNORE]")
    await repo.add_transcript(conv.id, "out", "  \n[IGNORE]\n ")
    await repo.add_transcript(conv.id, "out", "   ")
    await repo.add_transcript(conv.id, "out", "a real answer")
    await repo.add_transcript(conv.id, "out", "boom", status="failed")

    page, _ = await repo.transcript_page(conv.id)

    assert [r["content"] for r in page] == [
        "hello",
        "Developer task d335c034 has a question",
        "a real answer",
        "boom",
    ]

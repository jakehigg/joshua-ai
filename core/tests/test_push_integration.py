"""``repo.push_subscriptions`` against a real Postgres: one row per browser,
the endpoint that is unique, and what a failure does to a row."""

from __future__ import annotations

import pytest
from joshua_core.store.push import FAILURE_LIMIT

pytestmark = pytest.mark.integration

MIRA = "mira"
ALEX = "alex"
ENDPOINT = "https://push.test/send/one"
SECOND_ENDPOINT = "https://push.test/send/two"


@pytest.fixture
async def people(repo) -> None:
    await repo.upsert_person(MIRA, "Mira")
    await repo.upsert_person(ALEX, "Alex")


async def test_one_person_has_one_row_per_browser(repo, people) -> None:
    store = repo.push_subscriptions
    assert await store.list_for(MIRA) == []
    first = await store.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth", user_agent="a phone")
    await store.upsert(MIRA, SECOND_ENDPOINT, "another-p256dh", "another-auth")
    assert (first.person_id, first.user_agent, first.failures) == (MIRA, "a phone", 0)
    assert first.created_at is not None and first.last_ok_at is None
    rows = await store.list_for(MIRA)
    assert [row.endpoint for row in rows] == [ENDPOINT, SECOND_ENDPOINT]
    # Another person's browsers are not in that list.
    await store.upsert(ALEX, "https://push.test/send/three", "p", "a")
    assert len(await store.list_for(MIRA)) == 2


async def test_the_same_browser_again_updates_its_row(repo, people) -> None:
    store = repo.push_subscriptions
    await store.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth", user_agent="a phone")
    await store.mark_failure(ENDPOINT)
    again = await store.upsert(MIRA, ENDPOINT, "new-p256dh", "new-auth", user_agent="a laptop")
    assert (again.p256dh, again.user_agent, again.failures) == ("new-p256dh", "a laptop", 0)
    assert len(await store.list_for(MIRA)) == 1


async def test_a_delete_is_that_persons_alone(repo, people) -> None:
    store = repo.push_subscriptions
    await store.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth")
    assert await store.delete(ENDPOINT, person_id=ALEX) is False
    assert await store.delete(ENDPOINT, person_id=MIRA) is True
    assert await store.delete(ENDPOINT, person_id=MIRA) is False
    assert await store.list_for(MIRA) == []


async def test_a_success_notes_the_time_and_clears_the_failures(repo, people) -> None:
    store = repo.push_subscriptions
    await store.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth")
    await store.mark_failure(ENDPOINT)
    await store.mark_ok(ENDPOINT)
    row = (await store.list_for(MIRA))[0]
    assert row.last_ok_at is not None and row.failures == 0


async def test_a_row_over_the_threshold_is_dropped(repo, people) -> None:
    store = repo.push_subscriptions
    await store.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth")
    for attempt in range(1, FAILURE_LIMIT):
        assert await store.mark_failure(ENDPOINT) is False
        assert (await store.list_for(MIRA))[0].failures == attempt
    assert await store.mark_failure(ENDPOINT) is True
    assert await store.list_for(MIRA) == []
    # An endpoint that is already gone says so, and raises nothing.
    assert await store.mark_failure(ENDPOINT) is True


async def test_a_hard_delete_of_a_person_takes_their_subscriptions(repo, db, people) -> None:
    await repo.push_subscriptions.upsert(MIRA, ENDPOINT, "a-p256dh", "an-auth")
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM people WHERE id = %s", (MIRA,))
    assert await repo.push_subscriptions.list_for(MIRA) == []

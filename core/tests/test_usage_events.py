"""The `usage_event` rows: the recording functions, the fold by day, and each
call site that records. Fakes stand in for the database and the model."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
    make_settings,
    resolved,
)
from joshua_core.engine import describe as describe_module
from joshua_core.engine import internet as internet_agent
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.internet import do_lookup
from joshua_core.engine.types import Attachment
from joshua_core.engine.usage import Usage
from joshua_core.memory.nightly import NightlyReflector
from joshua_core.store.usage_events import (
    CALL_GROUPS,
    UsageEvent,
    clamp_days,
    record_usage,
    summarize,
    usage_features,
    window_start,
)
from joshua_shared import config as config_module
from joshua_shared import layout
from joshua_shared.attachments import AttachmentMeta, write_meta
from nightly_fakes import (
    ONESHOT_USAGE,
    FakeIndexer,
    FakeManager,
    FakeReflectRepo,
    canned_oneshot,
    row,
)
from nightly_fakes import make_settings as nightly_settings

CID = "6f1c2c1e-6a43-4c55-9d1e-6c1f0f6b2a10"


class Store:
    """Records each usage row."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[UsageEvent] = []
        self.fail = fail

    async def add_usage_event(self, event: UsageEvent) -> None:
        if self.fail:
            raise RuntimeError("database gone")
        self.events.append(event)


# --- the recording functions ---------------------------------------------------


async def test_one_row_for_each_call_with_its_ids() -> None:
    store = Store()
    used = [Usage(input=10, output=2), Usage(input=4, cache_read=7)]

    written = await record_usage(
        store, "reflection", used, model="m", person_id="alex", conversation_id=CID, ref="src"
    )

    assert written == 2
    assert [e.ref for e in store.events] == ["src:0", "src:1"]
    assert {(e.kind, e.name, e.model, e.person_id) for e in store.events} == {
        ("reflection", "reflection", "m", "alex")
    }
    assert store.events[0].conversation_id == CID
    assert store.events[1].usage == Usage(input=4, cache_read=7)


async def test_one_call_keeps_its_ref_and_no_ref_gets_a_new_one() -> None:
    store = Store()
    await record_usage(store, "chat", [Usage(input=1)], ref="transcript:7")
    await record_usage(store, "internet", [Usage(input=1)])
    await record_usage(store, "internet", [Usage(input=1)])

    refs = [e.ref for e in store.events]
    assert refs[0] == "transcript:7"
    assert refs[1].startswith("internet:") and refs[1] != refs[2]


async def test_a_conversation_id_that_is_not_a_uuid_is_dropped() -> None:
    store = Store()
    await record_usage(store, "chat", [Usage(input=1)], conversation_id="c1")
    assert store.events[0].conversation_id is None


async def test_a_failed_write_is_a_warning_and_not_an_error() -> None:
    written = await record_usage(Store(fail=True), "describe", [Usage(input=1)])
    assert written == 0
    # A store with no usage method is the same: nothing raises.
    assert await record_usage(object(), "describe", [Usage(input=1)]) == 0


async def test_an_unknown_kind_is_not_a_call_kind() -> None:
    with pytest.raises(ValueError):
        await record_usage(Store(), "job", [Usage(input=1)])
    with pytest.raises(ValueError):
        await record_usage(Store(), "nightly", [Usage(input=1)])


def test_usage_features_are_the_call_groups() -> None:
    settings = nightly_settings()
    assert usage_features(settings) == list(CALL_GROUPS)
    assert CALL_GROUPS == ("chat", "reflection", "internet", "describe")


# --- the fold by day -------------------------------------------------------------


def test_the_window_starts_at_local_midnight() -> None:
    now = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)  # 23:00 on the 19th in New York
    start = window_start(now, "America/New_York", 2)
    assert start.isoformat() == "2026-10-18T00:00:00-04:00"


def test_days_are_held_to_the_bounds() -> None:
    assert (clamp_days(0), clamp_days(30), clamp_days(9999)) == (1, 30, 366)


def _row(day: date, name: str, worker: str | None = None, **counts: int) -> dict[str, Any]:
    base = {"calls": 1, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    return {"day": day, "name": name, "worker": worker, **base, **counts}


def test_summarize_groups_by_day_and_name_newest_first() -> None:
    rows = [
        _row(date(2026, 10, 18), "chat", input=10, output=5),
        _row(date(2026, 10, 20), "chat", calls=2, input=1, cache_read=100),
        _row(date(2026, 10, 20), "extra", "lookup", calls=3, input=7),
        _row(date(2026, 10, 20), "extra", "editor", output=2),
    ]

    body = summarize(rows, timezone="America/New_York", days=30).model_dump()

    assert body["timezone"] == "America/New_York" and body["days"] == 30
    assert body["groups"] == ["chat", "extra"]
    assert [r["date"] for r in body["rows"]] == ["2026-10-20", "2026-10-18"]
    extra = body["rows"][0]["groups"]["extra"]
    assert (extra["calls"], extra["input"], extra["output"], extra["total"]) == (
        4,
        7,
        2,
        9,
    )
    assert extra["workers"]["lookup"]["calls"] == 3
    assert body["rows"][0]["groups"]["chat"]["total"] == 101
    assert body["rows"][0]["groups"]["chat"]["workers"] == {}
    assert body["totals"]["chat"]["total"] == 116
    assert body["totals"]["chat"]["calls"] == 3


def test_summarize_of_nothing_is_empty() -> None:
    body = summarize([], timezone="UTC", days=7).model_dump()
    assert body == {"timezone": "UTC", "days": 7, "groups": [], "rows": [], "totals": {}}


def test_summarize_lists_every_feature_with_zero_counts() -> None:
    features = ["chat", "reflection", "internet", "describe", "extra"]
    rows = [_row(date(2026, 10, 20), "chat", input=4), _row(date(2026, 10, 20), "bundle")]
    body = summarize(rows, timezone="UTC", days=7, features=features).model_dump()
    assert body["groups"] == [*features, "bundle"]
    assert list(body["totals"]) == body["groups"]
    zero = body["totals"]["describe"]
    assert zero == {
        "calls": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
        "models": [],
        "workers": {},
    }
    # A day holds only the groups with usage that day.
    assert list(body["rows"][0]["groups"]) == ["bundle", "chat"]


def test_summarize_collects_the_models_of_each_group_and_worker() -> None:
    day = date(2026, 10, 20)
    rows = [
        {**_row(day, "chat", input=1), "model": "m-b"},
        {**_row(day, "chat", input=1), "model": "m-a"},
        {**_row(day, "chat", input=1), "model": "m-a"},
        {**_row(day, "chat", input=1), "model": None},
        {**_row(day, "extra", "editor", output=1), "model": "m-editor"},
        {**_row(day, "extra", "internet", input=1), "model": "m-lookup"},
        {**_row(date(2026, 10, 19), "extra", "editor", output=1), "model": "m-old"},
    ]
    body = summarize(rows, timezone="UTC", days=7, features=["chat"]).model_dump()
    assert body["totals"]["chat"]["models"] == ["m-a", "m-b"]
    extra = body["totals"]["extra"]
    assert extra["models"] == ["m-editor", "m-lookup", "m-old"]
    assert extra["workers"]["editor"]["models"] == ["m-editor", "m-old"]
    assert extra["workers"]["internet"]["models"] == ["m-lookup"]
    assert body["rows"][0]["groups"]["extra"]["workers"]["editor"]["models"] == ["m-editor"]


# --- the call sites ----------------------------------------------------------------


def _manager(repo: FakeRepo, tmp_path: Path) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=make_settings(),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


async def test_a_chat_turn_writes_one_chat_row(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = _manager(repo, tmp_path)

    await manager.run_turn(make_channel(), make_conversation(conversation_id=CID), "hello")

    [event] = repo.usage_events
    assert (event.kind, event.name, event.person_id, event.conversation_id) == (
        "chat",
        "chat",
        "alex",
        CID,
    )
    # The ref names the out row of the turn, so the backfill never adds it again.
    assert event.ref == f"transcript:{len(repo.transcripts)}"
    assert event.model


async def test_a_usage_failure_never_fails_the_turn(tmp_path: Path) -> None:
    class BrokenRepo(FakeRepo):
        async def add_usage_event(self, event: Any) -> None:
            raise RuntimeError("database gone")

    repo = BrokenRepo()
    result = await _manager(repo, tmp_path).run_turn(make_channel(), make_conversation(), "hi")
    assert result.text
    assert repo.transcripts[-1]["direction"] == "out"


async def test_a_described_attachment_writes_one_describe_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(abs_path: Path, **kwargs: Any) -> AttachmentMeta | None:
        kwargs["usage_out"].append(Usage(input=900, output=40))
        return None

    monkeypatch.setattr("joshua_core.engine.manager.describe_attachment", fake)
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xd8\xff")
    write_meta(path, AttachmentMeta(mime="image/jpeg"))
    repo = FakeRepo()

    await _manager(repo, tmp_path).run_turn(
        make_channel(),
        make_conversation(conversation_id=CID),
        "look",
        attachments=[Attachment(path=rel, mime="image/jpeg", name=path.name)],
        person_id="alex",
    )

    describe_rows = [e for e in repo.usage_events if e.kind == "describe"]
    assert len(describe_rows) == 1
    assert describe_rows[0].usage == Usage(input=900, output=40)
    assert describe_rows[0].person_id == "alex"
    assert describe_rows[0].model == "claude-haiku-4-5"


async def test_describe_attachment_passes_usage_out_to_the_worker(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(bytes.fromhex("89504e470d0a1a0a"))
    write_meta(path, AttachmentMeta(mime="image/png"))
    seen: list[Any] = []

    async def run(**kwargs: Any) -> Any:
        seen.append(kwargs.get("usage_out"))
        kwargs["usage_out"].append(Usage(input=1))
        return None

    used: list[Usage] = []
    await describe_module.describe_attachment(
        path,
        mime="image/png",
        timeout_s=5,
        inference=resolved("describe"),
        runner=run,
        usage_out=used,
    )
    assert seen == [used] and used == [Usage(input=1)]


async def test_a_chat_lookup_writes_one_internet_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(question: str, **kwargs: Any) -> Any:
        kwargs["usage_out"].append(Usage(input=30, output=3))
        return None

    monkeypatch.setattr(internet_agent, "ask", fake)
    repo = FakeRepo()
    deps = ToolDeps(
        repo=repo,  # type: ignore[arg-type]
        conversation=make_conversation(conversation_id=CID),
        channel=make_channel(),
        tz="UTC",
        person_id="alex",
    )
    settings = config_module.parse(
        "name: T\ntimezone: UTC\npeople:\n  - id: alex\n    name: Alex\n"
        "internet:\n  enabled: true\n",
        env={},
        source="<test>",
    )

    await do_lookup(deps, settings=settings, question="when is the market open?")

    [event] = repo.usage_events
    assert (event.kind, event.person_id, event.conversation_id) == ("internet", "alex", CID)
    assert event.usage == Usage(input=30, output=3)


async def test_each_reflection_call_writes_one_reflection_row(tmp_path: Path) -> None:
    layout.bootstrap_wiki(tmp_path)
    layout.bootstrap_shared(tmp_path)
    layout.bootstrap_shared_profile("Test House", tmp_path)
    layout.bootstrap_person("alex", "Alex", tmp_path)
    day = date(2026, 10, 19)
    at = datetime(2026, 10, 19, 15, tzinfo=UTC)
    from joshua_core.store.models import Person

    repo = FakeReflectRepo(
        people=[Person(id="alex", display_name="Alex", role="member")],
        rows=[row("c1", "in", "word " * 100, at), row("c1", "out", "Noted.", at)],
        conv_meta={"c1": {"session_mode": "per_person", "person_id": "alex"}},
    )
    reflector = NightlyReflector(
        repo=repo,
        indexer=FakeIndexer(),
        manager=FakeManager(),
        settings=nightly_settings(),
        data_dir=tmp_path,
        oneshot=canned_oneshot(),
    )

    await reflector.reflect(target_date=day)

    assert repo.usage_events
    assert {e.kind for e in repo.usage_events} == {"reflection"}
    assert all(e.usage == ONESHOT_USAGE for e in repo.usage_events)

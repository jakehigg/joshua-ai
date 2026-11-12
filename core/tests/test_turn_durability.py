"""A streamed turn outlives its client, and core says what is pending.

The manager is real and runs the stub agent. The model call is held on an
event, so a test chooses when the turn ends. The consumer of the stream is a
task that the test cancels, as Starlette cancels the response of a client that
left.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
)
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.stub_agent import StubAgentSession
from joshua_core.engine.types import Attachment, TurnResult
from joshua_core.turns import Resolution, TurnService
from joshua_shared import config as config_module
from joshua_shared.contracts import Chat, Handle, TurnEvent

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
"""


class Held:
    """Replaces ``_run_session``: each call waits for its own release."""

    def __init__(self) -> None:
        self.started: list[asyncio.Event] = []
        self.release: list[asyncio.Event] = []

    async def __call__(self, mc: Any, prompt: str, on_delta: Any) -> TurnResult:
        index = len(self.started)
        self.started.append(asyncio.Event())
        self.release.append(asyncio.Event())
        await on_delta("partial ")
        await self.release[index].wait()
        return TurnResult(text=f"reply {index}", is_error=False)


class Rig:
    """One service whose resolution always lands on the same conversation."""

    def __init__(self, tmp_path: Path, *, hold: bool = True) -> None:
        self.repo = FakeRepo()
        self.held = Held()
        settings = config_module.parse(CONFIG, env={}, source="<test>")
        self.manager = ConversationManager(
            repo=self.repo,
            settings=settings,
            composer=FakeComposer(),
            derive_profile=fake_derive_profile,
            agent_backend="stub",
            data_dir=tmp_path,
        )
        if hold:
            self.manager._run_session = self.held  # type: ignore[method-assign]
        self.channel = make_channel("cli:alex")
        self.conversation = make_conversation("c1", "alex", channel_id="cli:alex")
        self.service = TurnService(
            repo=self.repo,
            settings=settings,
            manager=self.manager,
            deliverer=None,  # type: ignore[arg-type]
        )

        async def resolve(event: TurnEvent) -> Resolution:
            return Resolution(
                channel=self.channel,
                conversation=self.conversation,
                person=None,
                is_group=False,
                speaker=None,
            )

        self.service.resolve = resolve  # type: ignore[method-assign]

    def event(self, text: str) -> TurnEvent:
        return TurnEvent(
            channel="cli:alex",
            chat=Chat(kind="dm", id="alex"),
            handle=Handle(type="cli", id="alex"),
            text=text,
        )

    async def consume(self, text: str, frames: list[tuple[str, Any]]) -> None:
        """Read one streamed turn into ``frames``, the way the HTTP layer does."""
        response = await self.service.stream(self.event(text))
        async for chunk in response.body_iterator:  # type: ignore[union-attr]
            raw = chunk if isinstance(chunk, str) else chunk.decode()
            name, _, data = raw.strip().partition("\ndata: ")
            frames.append((name.removeprefix("event: "), json.loads(data)))


async def _until(predicate: Any) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _names(frames: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in frames]


async def test_a_client_that_leaves_does_not_cancel_the_turn(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    frames: list[tuple[str, Any]] = []
    reader = asyncio.create_task(rig.consume("keep going", frames))
    await _until(lambda: "delta" in _names(frames))

    reader.cancel()  # the phone locked
    with pytest.raises(asyncio.CancelledError):
        await reader
    # Nothing cancelled the turn: it is still pending, and it holds the lock.
    assert rig.manager.pending_for("c1")["state"] == "running"

    rig.held.release[0].set()
    await rig.service.drain()

    rows = rig.repo.transcripts
    assert [(r["direction"], r["content"]) for r in rows] == [
        ("in", "keep going"),
        ("out", "reply 0"),
    ]
    assert rows[1]["status"] == "ok"
    assert rig.manager.pending_for("c1") is None
    # The client was not abandoned, as the client of a cancelled turn is.
    assert rig.manager._pool["c1"].replace_pending is False
    assert _names(frames) == ["accepted", "status", "delta"]


async def test_a_connected_client_gets_every_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    frames: list[tuple[str, Any]] = []
    reader = asyncio.create_task(rig.consume("hi", frames))
    await _until(lambda: rig.held.started)
    rig.held.release[0].set()
    await reader
    assert _names(frames) == ["accepted", "status", "delta", "done"]
    assert frames[0][1]["turn_id"] == frames[3][1]["turn_id"]
    assert frames[3][1]["text"] == "reply 0"
    # The inbound row carries the id the stream gave.
    assert rig.repo.transcripts[0]["meta"]["turn_id"] == frames[0][1]["turn_id"]


async def test_a_second_turn_is_queued_then_running(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first: list[tuple[str, Any]] = []
    second: list[tuple[str, Any]] = []
    one = asyncio.create_task(rig.consume("first", first))
    await _until(lambda: rig.held.started)
    two = asyncio.create_task(rig.consume("second", second))
    await _until(lambda: ("status", {"state": "queued"}) in second)

    pending = rig.manager.pending_for("c1")
    assert pending["state"] == "queued"
    assert pending["text"] == "second"
    assert pending["since"]

    rig.held.release[0].set()
    await one
    await _until(lambda: len(rig.held.started) == 2)
    assert rig.manager.pending_for("c1")["state"] == "running"
    rig.held.release[1].set()
    await two
    assert [d["state"] for n, d in second if n == "status"] == ["queued", "running"]
    assert rig.manager.pending_for("c1") is None


async def test_a_queued_turn_survives_its_client_too(tmp_path: Path) -> None:
    """The incident: the phone sent a turn behind a task turn, then went away."""
    rig = Rig(tmp_path)
    task = asyncio.create_task(
        rig.manager.run_turn(rig.channel, rig.conversation, "do the chore", direction="task")
    )
    await _until(lambda: rig.held.started)
    frames: list[tuple[str, Any]] = []
    reader = asyncio.create_task(rig.consume("the photo", frames))
    await _until(lambda: ("status", {"state": "queued"}) in frames)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    rig.held.release[0].set()
    await task
    await _until(lambda: len(rig.held.started) == 2)
    rig.held.release[1].set()
    await rig.service.drain()

    out = [r for r in rig.repo.transcripts if r["direction"] == "out"]
    assert [r["content"] for r in out] == ["reply 0", "reply 1"]
    assert rig.manager.pending_for("c1") is None


async def test_a_task_turn_hides_its_text(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    task = asyncio.create_task(
        rig.manager.run_turn(rig.channel, rig.conversation, "secret prompt", direction="task")
    )
    await _until(lambda: rig.held.started)
    assert rig.manager.pending_for("c1")["text"] == ""
    rig.held.release[0].set()
    await task


async def test_a_failed_turn_leaves_no_pending(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def boom(mc: Any, prompt: str, on_delta: Any) -> TurnResult:
        raise RuntimeError("model down")

    rig.manager._run_session = boom  # type: ignore[method-assign]
    frames: list[tuple[str, Any]] = []
    await rig.consume("hi", frames)
    assert _names(frames)[-1] == "error"
    assert rig.manager.pending_for("c1") is None


async def test_a_real_cancel_still_abandons_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels the task. That keeps the cleanup of a cancelled turn."""
    rig = Rig(tmp_path, hold=False)
    started = asyncio.Event()

    async def hang(self: Any, prompt: str, on_delta: Any = None) -> TurnResult:
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("not reached")

    monkeypatch.setattr(StubAgentSession, "run", hang)
    task = asyncio.create_task(rig.manager.run_turn(rig.channel, rig.conversation, "x"))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.manager._pool["c1"].replace_pending is True
    assert rig.manager._pool["c1"].replace_reason == "turn cancelled"
    assert rig.manager.pending_for("c1") is None


async def test_the_inbound_row_records_the_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_describer(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no model in this test")

    monkeypatch.setattr("joshua_core.engine.manager.describe_attachment", no_describer)
    rig = Rig(tmp_path)
    rel = "people/alex/attachments/2026/11/photo.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    attachment = Attachment(
        path=rel, mime="image/jpeg", name="photo.jpg", original_name="IMG_1.jpg"
    )
    task = asyncio.create_task(
        rig.manager.run_turn(
            rig.channel, rig.conversation, "look", attachments=[attachment], person_id="alex"
        )
    )
    await _until(lambda: rig.held.started)
    rig.held.release[0].set()
    await task

    meta = rig.repo.transcripts[0]["meta"]
    assert meta["attachments"] == [
        {"path": rel, "mime": "image/jpeg", "name": "IMG_1.jpg", "size": 103}
    ]

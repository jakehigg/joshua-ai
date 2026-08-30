"""Telegram adapter tests: inbound guard, turn shape, attachments, and split send.

The tests drive the adapter with fake Telegram objects (update, message, bot) and
a fake core and pipeline, so no real bot token or network is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from joshua_channels.adapters.telegram import (
    NO_CAPTION_TEXT,
    TURN_FAILED,
    UNKNOWN_SENDER_REPLY,
    TelegramAdapter,
    split_message,
)
from joshua_channels.core_client import TurnAck
from joshua_channels.guard import Guard
from joshua_shared import config as config_module
from joshua_shared.contracts import Attachment, TurnEvent

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex Diaz
    handles:
      telegram: "12345"
groups:
  - id: everyone
    channel: telegram
    chat_id: "-100999"
channels:
  telegram:
    bot_token: t-token
    unknown_sender: reply
  limits:
    max_text_chars: 20
    per_handle_per_minute: 20
"""


def _cfg(text: str = CONFIG):
    return config_module.parse(text, env={}, source="<test>")


# --- fakes -----------------------------------------------------------------


class FakeFile:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    async def download_to_drive(self, path: Path) -> None:
        # Record the destination name; a real download writes bytes here.
        self._sink.append(Path(path).name)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.downloaded: list[str] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)

    async def send_chat_action(self, **kwargs: Any) -> None:
        self.actions.append(kwargs)

    async def send_photo(self, **kwargs: Any) -> None:
        self.photos.append(kwargs)

    async def send_document(self, **kwargs: Any) -> None:
        self.documents.append(kwargs)

    async def get_file(self, file_id: str) -> FakeFile:
        return FakeFile(self.downloaded)


@dataclass
class FakeCore:
    ack: TurnAck = field(default_factory=lambda: TurnAck(accepted=True, status=202, turn_id="t1"))
    events: list[TurnEvent] = field(default_factory=list)
    raise_exc: bool = False

    async def submit_turn(self, event: TurnEvent) -> TurnAck:
        if self.raise_exc:
            raise RuntimeError("core down")
        self.events.append(event)
        return self.ack


@dataclass
class FakePipeline:
    result: list[Attachment]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        self.calls.append({"dir": inbox_dir, "person_id": person_id, "group_id": group_id})
        return self.result


def _photo(size: int = 1000):
    small = SimpleNamespace(file_id="small", file_unique_id="us", file_size=10)
    large = SimpleNamespace(file_id="large", file_unique_id="ul", file_size=size)
    return [small, large]


def _document(name: str = "notes.pdf"):
    return SimpleNamespace(file_id="doc", file_unique_id="ud", file_name=name, file_size=2000)


def _update(
    *,
    user_id: int = 12345,
    full_name: str = "Alex Diaz",
    chat_id: str = "12345",
    chat_type: str = "private",
    chat_title: str | None = None,
    text: str | None = "hello",
    caption: str | None = None,
    photo: Any = None,
    document: Any = None,
    message_id: int = 777,
):
    message = SimpleNamespace(
        text=text,
        caption=caption,
        photo=photo,
        document=document,
        message_id=message_id,
    )
    user = SimpleNamespace(id=user_id, full_name=full_name)
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=chat_title)
    return SimpleNamespace(effective_message=message, effective_user=user, effective_chat=chat)


def _adapter(
    tmp_path: Path,
    *,
    core: FakeCore | None = None,
    pipeline: FakePipeline | None = None,
    cfg_text: str = CONFIG,
):
    cfg = _cfg(cfg_text)
    guard = Guard(cfg, cfg.channels.limits, config_provider=lambda: cfg)
    adapter = TelegramAdapter(
        bot_token="t-token",
        guard=guard,
        core_client=core,  # type: ignore[arg-type]
        settings_provider=lambda: cfg,
        pipeline=pipeline,
        data_dir=str(tmp_path),
    )
    return adapter


# --- inbound ---------------------------------------------------------------


async def test_dm_known_handle_submits_one_turn(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(), ctx)

    assert len(core.events) == 1
    event = core.events[0]
    assert event.channel == "telegram:12345"
    assert event.chat.kind == "dm"
    assert event.chat.id == "12345"
    assert event.chat.title == "Alex Diaz"
    assert event.handle is not None and event.handle.type == "telegram"
    assert event.handle.id == "12345"
    assert event.text == "hello"
    assert event.message_id == "777"
    assert ctx.bot.actions == [{"chat_id": "12345", "action": "typing"}]


async def test_group_unknown_handle_refused_zero_core(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    # An unconfigured group chat from an unknown sender.
    update = _update(user_id=55, chat_id="-100777", chat_type="supergroup", chat_title="Randoms")
    await adapter._on_message(update, ctx)

    assert core.events == []


async def test_unknown_sender_reply_policy_sends_notice(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    update = _update(user_id=999, chat_id="999")  # unknown private sender
    await adapter._on_message(update, ctx)

    assert core.events == []
    assert len(ctx.bot.messages) == 1
    assert ctx.bot.messages[0]["text"] == UNKNOWN_SENDER_REPLY
    assert ctx.bot.messages[0]["link_preview_options"].is_disabled is True


async def test_unknown_sender_drop_policy_stays_silent(tmp_path: Path) -> None:
    text = CONFIG.replace("unknown_sender: reply", "unknown_sender: drop")
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core, cfg_text=text)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(user_id=999, chat_id="999"), ctx)

    assert core.events == []
    assert ctx.bot.messages == []


async def test_unknown_sender_default_drops_silently(tmp_path: Path) -> None:
    # No unknown_sender key set: the default is drop, so an unknown sender gets
    # zero core calls and zero outbound send.
    text = CONFIG.replace("    unknown_sender: reply\n", "")
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core, cfg_text=text)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(user_id=999, chat_id="999"), ctx)

    assert core.events == []
    assert ctx.bot.messages == []


async def test_empty_message_is_skipped(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(text=None), ctx)

    assert core.events == []
    assert ctx.bot.actions == []


async def test_text_over_cap_is_truncated(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)  # max_text_chars = 20
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(text="x" * 50), ctx)

    assert core.events[0].text == "x" * 20


async def test_photo_downloaded_and_pipeline_invoked(tmp_path: Path) -> None:
    core = FakeCore()
    pipeline = FakePipeline(
        result=[Attachment(path="attachments/2026/08/ul.jpg", mime="image/jpeg", name="ul.jpg")]
    )
    adapter = _adapter(tmp_path, core=core, pipeline=pipeline)
    ctx = SimpleNamespace(bot=FakeBot())

    update = _update(text=None, caption="look", photo=_photo())
    await adapter._on_message(update, ctx)

    assert len(pipeline.calls) == 1
    assert pipeline.calls[0]["person_id"] == "alex"
    assert ctx.bot.downloaded == ["ul.jpg"]  # largest photo size, downloaded
    event = core.events[0]
    assert event.attachments[0].path.startswith("attachments/")
    assert event.text == "look"


async def test_attachment_only_gets_placeholder_text(tmp_path: Path) -> None:
    core = FakeCore()
    pipeline = FakePipeline(
        result=[Attachment(path="attachments/2026/08/ud.pdf", mime="application/pdf")]
    )
    adapter = _adapter(tmp_path, core=core, pipeline=pipeline)
    ctx = SimpleNamespace(bot=FakeBot())

    update = _update(text=None, document=_document())
    await adapter._on_message(update, ctx)

    assert ctx.bot.downloaded == ["notes.pdf"]  # original extension kept
    assert core.events[0].text == NO_CAPTION_TEXT


async def test_no_pipeline_skips_attachments(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = _adapter(tmp_path, core=core, pipeline=None)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(text=None, photo=_photo()), ctx)

    assert core.events[0].attachments == []
    assert core.events[0].text == NO_CAPTION_TEXT


async def test_submit_5xx_sends_failure_notice(tmp_path: Path) -> None:
    core = FakeCore(ack=TurnAck(accepted=False, status=502, reason="bad_gateway"))
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(), ctx)

    assert ctx.bot.messages[-1]["text"] == TURN_FAILED


async def test_submit_403_is_silent(tmp_path: Path) -> None:
    core = FakeCore(ack=TurnAck(accepted=False, status=403, reason="unknown_sender"))
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(), ctx)

    assert ctx.bot.messages == []


async def test_submit_exception_sends_failure_notice(tmp_path: Path) -> None:
    core = FakeCore(raise_exc=True)
    adapter = _adapter(tmp_path, core=core)
    ctx = SimpleNamespace(bot=FakeBot())

    await adapter._on_message(_update(), ctx)

    assert ctx.bot.messages[-1]["text"] == TURN_FAILED


# --- outbound --------------------------------------------------------------


def test_split_message_counts() -> None:
    assert split_message("") == []
    assert len(split_message("x" * 9000)) == 3
    assert len(split_message("short")) == 1


async def test_send_9000_chars_is_3_messages(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    bot = FakeBot()
    adapter._bot = bot

    parts = await adapter.send("12345", "y" * 9000, [])

    assert parts == 3
    assert len(bot.messages) == 3
    assert all(m["link_preview_options"].is_disabled is True for m in bot.messages)


async def test_send_routes_image_and_document(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    bot = FakeBot()
    adapter._bot = bot

    parts = await adapter.send("12345", "here", [Path("/data/a.png"), Path("/data/b.pdf")])

    assert parts == 3  # one text + two attachments
    assert len(bot.photos) == 1
    assert len(bot.documents) == 1


async def test_send_without_start_raises(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    with pytest.raises(RuntimeError):
        await adapter.send("12345", "hi", [])


# --- lifecycle -------------------------------------------------------------


class _FakeUpdater:
    def __init__(self) -> None:
        self.polling = False
        self.stopped = False
        self.error_callback: Any = None

    async def start_polling(self, error_callback: Any = None) -> None:
        self.polling = True
        self.error_callback = error_callback

    async def stop(self) -> None:
        self.stopped = True


class _FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.updater = _FakeUpdater()
        self.handlers: list[Any] = []
        self.steps: list[str] = []

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        self.steps.append("initialize")

    async def start(self) -> None:
        self.steps.append("start")

    async def stop(self) -> None:
        self.steps.append("stop")

    async def shutdown(self) -> None:
        self.steps.append("shutdown")


class _FakeBuilder:
    def __init__(self, application: _FakeApplication) -> None:
        self._application = application

    def token(self, _token: str) -> _FakeBuilder:
        return self

    def build(self) -> _FakeApplication:
        return self._application


async def test_start_and_stop_drive_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from joshua_channels.adapters import telegram as telegram_module

    application = _FakeApplication()
    monkeypatch.setattr(
        telegram_module.Application,
        "builder",
        classmethod(lambda cls: _FakeBuilder(application)),
    )
    adapter = _adapter(tmp_path)

    await adapter.start()
    assert application.steps == ["initialize", "start"]
    assert application.updater.polling is True
    assert len(application.handlers) == 1
    assert adapter._bot is application.bot

    await adapter.stop()
    assert application.updater.stopped is True
    assert application.steps == ["initialize", "start", "stop", "shutdown"]


async def test_stop_without_start_is_noop(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    await adapter.stop()  # no application built; must not raise


# --- health ----------------------------------------------------------------


async def test_health_ok_before_any_poll_error(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    health = await adapter.health()
    assert health.as_dict() == {"ok": True}


async def test_poll_error_sets_failing_and_names_conflict(tmp_path: Path) -> None:
    from telegram.error import Conflict

    adapter = _adapter(tmp_path)
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))

    body = (await adapter.health()).as_dict()
    assert body["ok"] is False
    assert "getUpdates" in body["reason"]
    assert body["errors"] == 2


async def test_poll_error_then_success_recovers(tmp_path: Path) -> None:
    from telegram.error import Conflict

    adapter = _adapter(tmp_path)
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))

    # A rolling update overlap ends and polling recovers.
    adapter._on_poll_ok()

    body = (await adapter.health()).as_dict()
    assert body["ok"] is True
    assert "reason" not in body
    assert body["errors"] == 2  # the past errors stay visible to the operator


async def test_inbound_message_marks_poll_ok(tmp_path: Path) -> None:
    from telegram.error import Conflict

    core = FakeCore()
    adapter = _adapter(tmp_path, core=core)
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))
    ctx = SimpleNamespace(bot=FakeBot())

    # A delivered update proves get_updates works again.
    await adapter._on_message(_update(), ctx)

    body = (await adapter.health()).as_dict()
    assert body["ok"] is True
    assert body["errors"] == 1


async def test_repeating_poll_error_stays_unhealthy(tmp_path: Path) -> None:
    from telegram.error import Conflict

    adapter = _adapter(tmp_path)
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))
    adapter._on_poll_ok()  # a brief recovery
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))
    adapter._on_poll_error(Conflict("terminated by other getUpdates request"))

    body = (await adapter.health()).as_dict()
    assert body["ok"] is False
    assert "getUpdates" in body["reason"]
    assert body["errors"] == 3


async def test_poll_error_redacts_bot_token(tmp_path: Path) -> None:
    from telegram.error import InvalidToken

    adapter = _adapter(tmp_path)  # bot_token is "t-token"
    adapter._on_poll_error(InvalidToken("bad token t-token rejected"))

    body = (await adapter.health()).as_dict()
    assert "t-token" not in body["reason"]
    assert "<redacted>" in body["reason"]


async def test_start_passes_error_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joshua_channels.adapters import telegram as telegram_module

    application = _FakeApplication()
    monkeypatch.setattr(
        telegram_module.Application,
        "builder",
        classmethod(lambda cls: _FakeBuilder(application)),
    )
    adapter = _adapter(tmp_path)
    await adapter.start()
    assert application.updater.error_callback == adapter._on_poll_error


# --- resolve ---------------------------------------------------------------


def test_resolve_ref_dm_and_group(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter.resolve_ref("dm:alex") == "12345"
    assert adapter.resolve_ref("group:everyone") == "-100999"
    assert adapter.resolve_ref("dm:nobody") is None
    assert adapter.resolve_ref("group:nope") is None
    assert adapter.resolve_ref("weird") is None

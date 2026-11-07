"""The peer receive path: bot first, every refusal, replay, staging, acks, and core notify."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from joshua_channels.adapters.telegram import TelegramAdapter
from joshua_channels.peers import receive
from joshua_channels.peers.outbox import Outbox
from joshua_channels.peers.protocol import bot_sender
from joshua_channels.peers.receive import PeerReceiver, RateLimit, _lenient_item_id
from joshua_shared import layout
from joshua_shared.peers import (
    MAX_ATTACHMENT_BYTES,
    MAX_ENVELOPE_BYTES,
    AckEnvelope,
    parse_envelope,
)
from peer_fakes import (
    NOW,
    PEER_BOT,
    QUIET_BOT,
    FakeBot,
    FakeClock,
    FakePeerCore,
    ack,
    bot_update,
    cfg,
    document,
    item,
    raw,
    sha,
    ulid,
)


class Rig:
    """One receiver on a temporary volume, with a fake bot, clock, and core."""

    def __init__(
        self,
        root: Path,
        *,
        core: FakePeerCore | None = None,
        clock: FakeClock | None = None,
        rate_limit: RateLimit | None = None,
        no_core: bool = False,
    ) -> None:
        self.root = root
        self.cfg = cfg()
        self.clock = clock or FakeClock()
        self.core = core or FakePeerCore()
        self.bot = FakeBot()
        self.outbox_sent: list[tuple[str, bytes, str]] = []

        async def outbox_send(chat_id: str, data: bytes, filename: str) -> None:
            self.outbox_sent.append((chat_id, data, filename))

        self.outbox = Outbox(root, sender=lambda: outbox_send, clock=self.clock)
        self.receiver = PeerReceiver(
            data_dir=root,
            settings_provider=lambda: self.cfg,
            outbox=self.outbox,
            core_client=None if no_core else self.core,  # type: ignore[arg-type]
            clock=self.clock,
            rate_limit=rate_limit,
        )
        self._n = 0

    async def doc(
        self,
        name: str,
        data: bytes,
        *,
        user_id: int = PEER_BOT,
        chat_type: str = "private",
        size: int | None = -1,
    ) -> str:
        self._n += 1
        file_id = f"file-{self._n}"
        self.bot.files[file_id] = data
        update = bot_update(
            document(file_id, name, len(data) if size == -1 else size),
            user_id=user_id,
            chat_type=chat_type,
        )
        return await self.receiver.handle(
            update.effective_message,
            update.effective_user,
            update.effective_chat,
            self.bot,
            bot_sender(self.bot),
        )

    async def envelope(self, envelope: dict[str, Any], **kwargs: Any) -> str:
        return await self.doc("envelope.json", raw(envelope), **kwargs)

    def acks(self) -> list[AckEnvelope]:
        out = []
        for sent in self.bot.documents:
            assert sent["filename"] == "envelope.json"
            parsed = parse_envelope(sent["document"])
            assert isinstance(parsed, AckEnvelope)
            out.append(parsed)
        return out

    def inbox(self) -> list[str]:
        root = layout.peers_inbox_root(self.root)
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


# --- an accepted item -------------------------------------------------------


async def test_an_item_for_a_known_person_is_stored_acked_and_core_is_told(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    envelope = item(ulid(1))
    data = raw(envelope)

    result = await rig.doc("envelope.json", data)

    assert result == receive.ACCEPTED
    folder = layout.peer_item_dir(ulid(1), tmp_path)
    assert (folder / "envelope.json").read_bytes() == data
    assert (folder / "body.md").read_text() == "Braise for **three** hours."
    meta = json.loads((folder / "meta.json").read_text())
    assert meta == {
        "peer_id": "rivera",
        "received_at": NOW.isoformat(),
        "recipient": "alex",
        "reason": "exact",
        "candidates": [],
        "status": "new",
    }
    acks = rig.acks()
    assert [(a.body.ref, a.body.status) for a in acks] == [(ulid(1), "accepted")]
    assert rig.bot.documents[0]["chat_id"] == str(PEER_BOT)
    assert rig.core.received == [ulid(1)]
    assert rig.bot.messages == []


async def test_an_item_for_nobody_here_is_unmatched_and_still_stored(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1), to="Nobody Known"))

    assert result == receive.UNMATCHED
    meta = json.loads((layout.peer_item_dir(ulid(1), tmp_path) / "meta.json").read_text())
    assert meta["recipient"] is None
    assert meta["reason"] == "no_match"
    assert rig.acks()[0].body.status == "unmatched"
    assert rig.core.received == [ulid(1)]


# --- drops before any parse -------------------------------------------------


async def test_an_unpaired_bot_is_dropped_with_no_reply_and_no_download(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1)), user_id=4242)

    assert result == receive.UNPAIRED
    assert rig.bot.get_file_calls == []
    assert rig.bot.documents == [] and rig.bot.messages == []
    assert rig.inbox() == []
    assert rig.core.received == []


async def test_an_unpaired_bot_log_holds_the_bot_id_and_no_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(tmp_path)
    caplog.set_level("INFO")

    await rig.envelope(item(ulid(1), markdown="SECRET-BODY"), user_id=4242)

    text = caplog.text
    assert "4242" in text
    assert "SECRET-BODY" not in text


async def test_a_paired_bot_in_a_group_chat_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1)), chat_type="group")

    assert result == receive.NOT_PRIVATE
    assert rig.bot.get_file_calls == []
    assert rig.bot.documents == []
    assert rig.inbox() == []


async def test_a_text_message_from_a_peer_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    update = bot_update(None, text="hello")

    result = await rig.receiver.handle(
        update.effective_message,
        update.effective_user,
        update.effective_chat,
        rig.bot,
        bot_sender(rig.bot),
    )

    assert result == receive.NO_DOCUMENT
    assert rig.bot.documents == []


async def test_a_peer_over_the_rate_limit_is_dropped(tmp_path: Path) -> None:
    ticks = [0.0]
    rig = Rig(tmp_path, rate_limit=RateLimit(limit=2, window_s=60, monotonic=lambda: ticks[0]))

    assert await rig.envelope(item(ulid(1))) == receive.ACCEPTED
    assert await rig.envelope(item(ulid(2))) == receive.ACCEPTED
    assert await rig.envelope(item(ulid(3))) == receive.RATE_LIMITED
    assert rig.inbox() == [ulid(1), ulid(2)]

    ticks[0] = 61.0
    assert await rig.envelope(item(ulid(3))) == receive.ACCEPTED


def test_the_default_rate_limit_is_per_peer() -> None:
    limit = RateLimit(limit=1, monotonic=lambda: 5.0)
    assert limit.allow("rivera")
    assert not limit.allow("rivera")
    assert limit.allow("quiet")


# --- envelope refusals -----------------------------------------------------


async def test_an_envelope_over_the_size_limit_is_refused_before_download(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1)), size=MAX_ENVELOPE_BYTES + 1)

    assert result == receive.OVERSIZE
    assert rig.bot.get_file_calls == []
    assert rig.inbox() == []


async def test_an_envelope_that_is_larger_than_declared_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    big = item(ulid(1), markdown="x")
    data = raw(big) + b" " * (MAX_ENVELOPE_BYTES + 1)

    result = await rig.doc("envelope.json", data, size=None)

    assert result == receive.OVERSIZE
    assert rig.bot.documents == []
    assert rig.inbox() == []


async def test_a_bad_schema_with_an_id_is_refused_with_a_rejected_ack(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    envelope = item(ulid(1))
    envelope["extra"] = "field"

    result = await rig.envelope(envelope)

    assert result == receive.INVALID
    assert [(a.body.ref, a.body.status) for a in rig.acks()] == [(ulid(1), "rejected")]
    assert rig.inbox() == []
    assert rig.core.received == []


async def test_a_bad_schema_with_no_id_gets_no_ack(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.doc("envelope.json", b"not json")

    assert result == receive.INVALID
    assert rig.bot.documents == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"[1]", None),
        (b'{"id": "nope"}', None),
        (b'{"id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "kind": "ack"}', None),
        (b'{"id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "kind": "recipe"}', "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
    ],
)
def test_the_lenient_id_reads_only_a_ulid_of_an_item(data: bytes, expected: str | None) -> None:
    assert _lenient_item_id(data) == expected


async def test_a_kind_the_peer_may_not_send_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1), kind="link"))

    assert result == receive.KIND_NOT_ALLOWED
    assert rig.acks()[0].body.status == "rejected"
    assert rig.inbox() == []
    assert rig.core.received == []


async def test_an_expired_item_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    envelope = item(ulid(1), sent_at=NOW - timedelta(days=8), expires_at=NOW - timedelta(seconds=1))

    result = await rig.envelope(envelope)

    assert result == receive.EXPIRED
    assert rig.acks()[0].body.status == "rejected"
    assert rig.inbox() == []


async def test_an_item_that_expires_exactly_now_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1), sent_at=NOW - timedelta(days=1), expires_at=NOW))

    assert result == receive.EXPIRED


# --- replay ------------------------------------------------------------------


async def test_a_replay_is_not_stored_twice_and_gets_the_original_ack(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    envelope = item(ulid(1))

    assert await rig.envelope(envelope) == receive.ACCEPTED
    assert await rig.envelope(envelope) == receive.REPLAY

    assert rig.inbox() == [ulid(1)]
    assert rig.core.received == [ulid(1)]
    first, second = rig.bot.documents
    assert first["document"] == second["document"]


async def test_a_replay_of_a_refused_item_resends_the_rejected_ack(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    envelope = item(ulid(1), kind="link")

    await rig.envelope(envelope)
    assert await rig.envelope(envelope) == receive.REPLAY

    assert [a.body.status for a in rig.acks()] == ["rejected", "rejected"]


async def test_the_replay_table_survives_a_restart(tmp_path: Path) -> None:
    first = Rig(tmp_path)
    await first.envelope(item(ulid(1)))

    second = Rig(tmp_path)
    assert await second.envelope(item(ulid(1))) == receive.REPLAY
    assert second.core.received == []


async def test_a_stored_item_with_no_replay_record_is_still_a_replay(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.envelope(item(ulid(1)))
    layout.peer_seen_path(ulid(1), tmp_path).unlink()

    assert await rig.envelope(item(ulid(1))) == receive.REPLAY
    assert rig.acks()[-1].body.status == "accepted"
    assert layout.peer_seen_path(ulid(1), tmp_path).exists()
    assert rig.core.received == [ulid(1)]


async def test_the_replay_table_prunes_a_record_after_it_expires(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.envelope(item(ulid(1), expires_at=NOW + timedelta(hours=1)))

    assert rig.receiver.seen.prune(NOW) == 0
    assert rig.receiver.seen.prune(NOW + timedelta(hours=1)) == 1
    assert rig.receiver.seen.get(ulid(1)) is None


# --- attachments ---------------------------------------------------------------


def _attachment(name: str, data: bytes, *, size: int | None = None) -> dict[str, Any]:
    return {"name": name, "mime": "image/jpeg", "sha256": sha(data), "size": size or len(data)}


async def test_a_staged_attachment_is_stored_with_its_item(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    photo = b"\xff\xd8jpeg-bytes"

    assert await rig.doc("photo.jpg", photo) == receive.STAGED
    result = await rig.envelope(item(ulid(1), attachments=[_attachment("ribs.jpg", photo)]))

    assert result == receive.ACCEPTED
    stored = layout.peer_item_dir(ulid(1), tmp_path) / "attachments" / "ribs.jpg"
    assert stored.read_bytes() == photo
    assert rig.receiver.staging.path("rivera", sha(photo)) is None
    assert [a.body.status for a in rig.acks()] == ["accepted"]


async def test_a_missing_attachment_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.envelope(item(ulid(1), attachments=[_attachment("a.jpg", b"never sent")]))

    assert result == receive.ATTACHMENT_MISMATCH
    assert rig.acks()[0].body.status == "rejected"
    assert rig.inbox() == []


async def test_an_attachment_with_the_wrong_size_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    photo = b"photo"
    await rig.doc("photo.jpg", photo)

    result = await rig.envelope(item(ulid(1), attachments=[_attachment("a.jpg", photo, size=99)]))

    assert result == receive.ATTACHMENT_MISMATCH
    assert rig.inbox() == []


async def test_an_attachment_staged_by_another_peer_does_not_count(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    photo = b"photo"
    await rig.doc("photo.jpg", photo, user_id=QUIET_BOT)

    result = await rig.envelope(item(ulid(1), attachments=[_attachment("a.jpg", photo)]))

    assert result == receive.ATTACHMENT_MISMATCH


async def test_a_staged_attachment_older_than_the_ttl_is_gone(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    photo = b"photo"
    await rig.doc("photo.jpg", photo)
    rig.clock.advance(hours=2)

    result = await rig.envelope(item(ulid(1), attachments=[_attachment("a.jpg", photo)]))

    assert result == receive.ATTACHMENT_MISMATCH


async def test_an_attachment_over_the_size_limit_is_not_downloaded(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    result = await rig.doc("big.bin", b"x", size=MAX_ATTACHMENT_BYTES + 1)

    assert result == receive.ATTACHMENT_TOO_LARGE
    assert rig.bot.get_file_calls == []


async def test_an_attachment_with_no_declared_size_is_not_downloaded(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    assert await rig.doc("x.bin", b"x", size=None) == receive.ATTACHMENT_TOO_LARGE


async def test_staging_keeps_at_most_the_cap(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from joshua_channels.peers import store

    monkeypatch.setattr(store, "STAGING_MAX_FILES", 2)
    rig = Rig(tmp_path)
    for n in range(3):
        rig.clock.advance(seconds=1)
        await rig.doc(f"{n}.bin", f"file-{n}".encode())

    staged = sorted(p.name for p in rig.receiver.staging.dir("rivera").iterdir())
    assert staged == sorted([sha(b"file-1"), sha(b"file-2")])


# --- acks ----------------------------------------------------------------------


def _queue(rig: Rig, item_id: str, peer_id: str = "rivera", chat_id: str = "900") -> None:
    from joshua_shared.peers import ItemEnvelope

    envelope = ItemEnvelope.model_validate(item(item_id, sender="alex"))
    rig.outbox.enqueue(peer_id, chat_id, envelope, [])


async def test_an_ack_settles_the_outbox_and_never_calls_core_or_replies(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _queue(rig, ulid(5))

    result = await rig.envelope(ack(ulid(6), ulid(5)))

    assert result == receive.ACK
    state = rig.outbox.state(ulid(5))
    assert state is not None
    assert (state["status"], state["ack_status"]) == ("acked", "accepted")
    assert rig.core.received == []
    assert rig.bot.documents == [] and rig.bot.messages == []
    assert rig.inbox() == []


async def test_an_ack_for_an_unknown_ref_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    assert await rig.envelope(ack(ulid(6), ulid(7))) == receive.ACK_DROPPED
    assert rig.bot.documents == []
    assert rig.core.received == []


async def test_an_ack_from_the_wrong_peer_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _queue(rig, ulid(5))

    result = await rig.envelope(ack(ulid(6), ulid(5)), user_id=QUIET_BOT)

    assert result == receive.ACK_DROPPED
    state = rig.outbox.state(ulid(5))
    assert state is not None and state["status"] == "pending"


# --- core and ack failures -------------------------------------------------------


async def test_a_failed_core_notify_keeps_the_item(tmp_path: Path) -> None:
    rig = Rig(tmp_path, core=FakePeerCore(raise_exc=True))

    assert await rig.envelope(item(ulid(1))) == receive.ACCEPTED
    assert rig.inbox() == [ulid(1)]
    assert rig.acks()[0].body.status == "accepted"


@pytest.mark.parametrize("status", [404, 500])
async def test_a_core_refusal_keeps_the_item(tmp_path: Path, status: int) -> None:
    rig = Rig(tmp_path, core=FakePeerCore(status=status))

    assert await rig.envelope(item(ulid(1))) == receive.ACCEPTED
    assert rig.inbox() == [ulid(1)]


async def test_no_core_client_keeps_the_item(tmp_path: Path) -> None:
    rig = Rig(tmp_path, no_core=True)

    assert await rig.envelope(item(ulid(1))) == receive.ACCEPTED
    assert rig.inbox() == [ulid(1)]


async def test_a_failed_ack_send_keeps_the_item(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bot.fail_send = True

    assert await rig.envelope(item(ulid(1))) == receive.ACCEPTED
    assert rig.inbox() == [ulid(1)]
    assert rig.core.received == [ulid(1)]


async def test_a_failed_download_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def broken(file_id: str) -> None:
        raise RuntimeError("no file")

    rig.bot.get_file = broken  # type: ignore[method-assign]
    assert await rig.envelope(item(ulid(1))) == receive.DOWNLOAD_FAILED
    assert await rig.doc("a.bin", b"a") == receive.DOWNLOAD_FAILED
    assert rig.inbox() == []


# --- the Telegram adapter: the bot check comes first ---------------------------------


class SpyGuard:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("a bot must never reach the guard")


class SpyCore:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def submit_turn(self, event: Any) -> Any:
        self.events.append(event)


def _adapter(tmp_path: Path, guard: SpyGuard, core: SpyCore, peers: Any = None) -> TelegramAdapter:
    config = cfg()
    return TelegramAdapter(
        bot_token="t-token",
        guard=guard,  # type: ignore[arg-type]
        core_client=core,  # type: ignore[arg-type]
        settings_provider=lambda: config,
        data_dir=str(tmp_path),
        peers=peers,
    )


async def test_a_bot_never_reaches_the_guard_and_gets_no_reply_with_no_peers(
    tmp_path: Path,
) -> None:
    guard, core, bot = SpyGuard(), SpyCore(), FakeBot()
    adapter = _adapter(tmp_path, guard, core)
    update = bot_update(None, user_id=4242, text="hello, I am a bot")

    await adapter._on_message(update, type("Ctx", (), {"bot": bot})())  # type: ignore[arg-type]

    assert guard.calls == 0
    assert core.events == []
    assert bot.messages == [] and bot.documents == [] and bot.actions == []


async def test_a_paired_bot_goes_to_the_receiver_and_never_to_the_guard(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    guard, core = SpyGuard(), SpyCore()
    adapter = _adapter(tmp_path, guard, core, peers=rig.receiver)
    rig.bot.files["f1"] = raw(item(ulid(1)))
    update = bot_update(document("f1", "envelope.json", None))

    await adapter._on_message(update, type("Ctx", (), {"bot": rig.bot})())  # type: ignore[arg-type]

    assert guard.calls == 0
    assert core.events == []
    assert rig.inbox() == [ulid(1)]
    assert rig.bot.messages == []
    assert [a.body.status for a in rig.acks()] == ["accepted"]


async def test_an_unpaired_bot_through_the_adapter_gets_no_reply(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    guard, core = SpyGuard(), SpyCore()
    adapter = _adapter(tmp_path, guard, core, peers=rig.receiver)
    update = bot_update(None, user_id=4242, text="hi")

    await adapter._on_message(update, type("Ctx", (), {"bot": rig.bot})())  # type: ignore[arg-type]

    assert guard.calls == 0
    assert rig.bot.messages == [] and rig.bot.documents == []


async def test_send_document_bytes_sends_through_the_bot(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, SpyGuard(), SpyCore())
    with pytest.raises(RuntimeError):
        await adapter.send_document_bytes("900", b"{}", "envelope.json")
    bot = FakeBot()
    adapter._bot = bot
    await adapter.send_document_bytes("900", b"{}", "envelope.json")
    assert bot.documents == [{"chat_id": "900", "document": b"{}", "filename": "envelope.json"}]

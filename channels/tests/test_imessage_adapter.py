"""iMessage adapter tests: the webhook route, the inbound guard, the attachment
pipeline, split send, and resolve.

The adapter runs with a fake BlueBubbles client, a fake core, and a fake
pipeline, so no test needs a Mac or a network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from imessage_helpers import (
    SECRET,
    FakeBB,
    FakeCore,
    FakePipeline,
    cfg,
    load_data,
    load_envelope,
    make_adapter,
)
from joshua_channels.adapters.imessage.adapter import PingCache
from joshua_channels.adapters.imessage.forwarder import NO_CAPTION_TEXT, UNKNOWN_SENDER_REPLY
from joshua_channels.adapters.imessage.normalize import InboundMessage, normalize_message
from joshua_channels.app import build_app
from joshua_channels.core_client import TurnAck
from joshua_channels.deliver import ChannelsContext
from joshua_channels.registry import AdapterRegistry

MAX_BYTES = 26214400


def _msg(name: str) -> InboundMessage:
    result = normalize_message(load_data(name), max_attachment_bytes=MAX_BYTES)
    assert isinstance(result, InboundMessage)
    return result


def _app_client(adapter=None) -> httpx.AsyncClient:
    registry = AdapterRegistry()
    if adapter is not None:
        registry.register(adapter)
    context = ChannelsContext(settings=cfg(), registry=registry)
    app = build_app(context)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


# ── webhook route ────────────────────────────────────────────────────────────


async def test_webhook_bad_secret_404_and_no_processing(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    async with _app_client(adapter) as client:
        resp = await client.post("/webhook/imessage/wrong", json=load_envelope("dm_text"))
    assert resp.status_code == 404
    assert adapter._forwarder.depth == 0  # nothing enqueued


async def test_webhook_without_imessage_adapter_is_404(tmp_path: Path) -> None:
    async with _app_client(None) as client:
        resp = await client.post(f"/webhook/imessage/{SECRET}", json=load_envelope("dm_text"))
    assert resp.status_code == 404


async def test_webhook_good_secret_drops_from_me(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    async with _app_client(adapter) as client:
        resp = await client.post(f"/webhook/imessage/{SECRET}", json=load_envelope("from_me_group"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "dropped:from_me"}


async def test_webhook_ignores_other_event_types(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    async with _app_client(adapter) as client:
        resp = await client.post(
            f"/webhook/imessage/{SECRET}", json={"type": "updated-message", "data": {}}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ignored": "updated-message"}


async def test_webhook_invalid_json_is_200(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    async with _app_client(adapter) as client:
        resp = await client.post(
            f"/webhook/imessage/{SECRET}",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 200  # BB never retries; never make it try
    assert resp.json()["ok"] is False


# ── inbound guard + turn shape ───────────────────────────────────────────────


async def test_known_dm_submits_one_turn(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = make_adapter(tmp_path, core=core)
    await adapter._forwarder.deliver(_msg("dm_text"))

    assert len(core.events) == 1
    event = core.events[0]
    assert event.channel == "imessage:iMessage;-;+15551234567"
    assert event.chat.kind == "dm"
    assert event.chat.id == "iMessage;-;+15551234567"
    assert event.handle is not None and event.handle.type == "imessage"
    assert event.handle.id == "+15551234567"
    assert event.text == "hey"
    assert event.message_id == "AABBCCDD-1111-2222-3333-444455556666"


async def test_unknown_sender_dropped_zero_core(tmp_path: Path) -> None:
    core = FakeCore()
    bb = FakeBB()
    adapter = make_adapter(tmp_path, core=core, bb=bb)
    # sms_handle is a DM from a handle no person owns.
    await adapter._forwarder.deliver(_msg("sms_handle"))

    assert core.events == []
    assert adapter._forwarder.refused == 1
    assert bb.sent == []  # drop policy stays silent


async def test_group_unknown_sender_allowed_as_group(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = make_adapter(tmp_path, core=core)
    await adapter._forwarder.deliver(_msg("group_text"))

    assert len(core.events) == 1
    event = core.events[0]
    assert event.chat.kind == "group"
    assert event.chat.title == "Everyone"
    assert event.handle.id == "mia.example@example.com"


async def test_text_over_cap_is_truncated(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = make_adapter(tmp_path, core=core)  # max_text_chars = 20
    message = InboundMessage(
        message_guid="g1",
        chat_guid="iMessage;-;+15551234567",
        is_group=False,
        sender_address="+15551234567",
        sender_service="iMessage",
        text="x" * 50,
        chat_display_name=None,
        date_created=0,
    )
    await adapter._forwarder.deliver(message)
    assert core.events[0].text == "x" * 20


async def test_heic_attachment_runs_pipeline(tmp_path: Path) -> None:
    core = FakeCore()
    bb = FakeBB(blobs={"att-guid-0001": b"heic-bytes"})
    from joshua_shared.contracts import Attachment

    pipeline = FakePipeline(
        result=[
            Attachment(
                path="attachments/2026/08/IMG_0001.jpg", mime="image/jpeg", name="IMG_0001.jpg"
            )
        ]
    )
    adapter = make_adapter(tmp_path, core=core, bb=bb, pipeline=pipeline)

    await adapter._forwarder.deliver(_msg("attachment"))

    # Only the in-size HEIC is downloaded; the oversize MOV was skipped.
    inbox = tmp_path / "inbox" / "CCDDEEFF-1111-2222-3333-444455556666"
    assert (inbox / "IMG_0001.HEIC").read_bytes() == b"heic-bytes"
    assert not (inbox / "MOV_0002.MOV").exists()
    assert pipeline.calls[0]["person_id"] == "alex"
    event = core.events[0]
    assert event.attachments[0].mime == "image/jpeg"
    assert event.text == NO_CAPTION_TEXT  # the placeholder text normalized to ""


async def test_no_pipeline_skips_attachments(tmp_path: Path) -> None:
    core = FakeCore()
    adapter = make_adapter(tmp_path, core=core, bb=FakeBB(), pipeline=None)
    await adapter._forwarder.deliver(_msg("attachment"))
    assert core.events[0].attachments == []
    assert core.events[0].text == NO_CAPTION_TEXT


async def test_download_failure_drops_only_that_attachment(tmp_path: Path) -> None:
    core = FakeCore()
    bb = FakeBB(fail_download={"att-guid-0001"})
    from joshua_shared.contracts import Attachment

    pipeline = FakePipeline(result=[Attachment(path="attachments/x.jpg", mime="image/jpeg")])
    adapter = make_adapter(tmp_path, core=core, bb=bb, pipeline=pipeline)

    await adapter._forwarder.deliver(_msg("attachment"))
    # The only downloadable attachment failed → nothing written → pipeline skipped.
    assert pipeline.calls == []
    assert core.events[0].attachments == []


async def test_unknown_sender_reply_policy_sends_notice(tmp_path: Path) -> None:
    core = FakeCore()
    bb = FakeBB()
    adapter = make_adapter(tmp_path, core=core, bb=bb, cfg_text=_CONFIG_REPLY)

    await adapter._forwarder.deliver(_msg("sms_handle"))

    assert core.events == []
    assert bb.sent == [("SMS;-;+15559998888", [UNKNOWN_SENDER_REPLY])]


async def test_core_failure_counts_as_failed(tmp_path: Path) -> None:
    core = FakeCore(raise_exc=True)
    adapter = make_adapter(tmp_path, core=core)
    ok = await adapter._forwarder.deliver(_msg("dm_text"))
    assert ok is False
    assert adapter._forwarder.failed == 1


async def test_turn_not_accepted_counts_as_failed(tmp_path: Path) -> None:
    core = FakeCore(ack=TurnAck(accepted=False, status=403, reason="unknown_sender"))
    adapter = make_adapter(tmp_path, core=core)
    ok = await adapter._forwarder.deliver(_msg("dm_text"))
    assert ok is False
    assert adapter._forwarder.failed == 1


# ── outbound send ────────────────────────────────────────────────────────────


async def test_send_chunks_text_and_one_attachment(tmp_path: Path) -> None:
    bb = FakeBB()
    adapter = make_adapter(tmp_path, bb=bb)
    photo = tmp_path / "pic.jpg"
    photo.write_bytes(b"x")

    parts = await adapter.send("iMessage;-;+15551234567", "y" * 9000, [photo])

    assert parts == 4  # 9000 chars / 4000 = 3 chunks + 1 attachment
    assert bb.sent[0][0] == "iMessage;-;+15551234567"
    assert len(bb.sent[0][1]) == 3
    assert bb.attachments_sent == [("iMessage;-;+15551234567", photo)]


async def test_send_attachment_failure_falls_back_to_a_note(tmp_path: Path) -> None:
    bb = FakeBB(fail_attachment=True)
    adapter = make_adapter(tmp_path, bb=bb)
    photo = tmp_path / "pic.jpg"
    photo.write_bytes(b"x")

    parts = await adapter.send("chat1", "hi", [photo])

    assert parts == 2  # one text chunk + the fallback note
    assert bb.attachments_sent == []  # the send failed
    assert bb.sent[-1] == ("chat1", ["(could not send attachment: pic.jpg)"])


async def test_send_empty_text_no_attachments_is_zero_parts(tmp_path: Path) -> None:
    bb = FakeBB()
    adapter = make_adapter(tmp_path, bb=bb)
    assert await adapter.send("chat1", "", []) == 0
    assert bb.sent == []


# ── resolve ──────────────────────────────────────────────────────────────────


def test_resolve_ref_dm_and_group(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    assert adapter.resolve_ref("dm:alex") == "iMessage;-;+15551234567"
    assert adapter.resolve_ref("group:everyone") == "iMessage;+;chat100000000000000001"
    assert adapter.resolve_ref("dm:nobody") is None
    assert adapter.resolve_ref("group:nope") is None
    assert adapter.resolve_ref("weird") is None


# ── readiness + lifecycle ────────────────────────────────────────────────────


async def test_readyz_reports_bluebubbles_reachability(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, bb=FakeBB(reachable=True))
    async with _app_client(adapter) as client:
        body = (await client.get("/readyz")).json()
    assert body["ok"] is True
    assert body["checks"]["imessage"] == {"ok": True, "bluebubbles": True}


async def test_start_and_stop_are_clean(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)  # reconcile disabled, coalesce off
    await adapter.start()
    await adapter.stop()


async def test_ping_cache_caches_within_ttl() -> None:
    bb = FakeBB(reachable=True)
    cache = PingCache(bb, ttl_s=1000.0)
    assert await cache.reachable() is True
    bb.reachable_value = False  # a live flap
    assert await cache.reachable() is True  # still the cached value


_CONFIG_REPLY = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex Diaz
    handles:
      imessage: "+15551234567"
channels:
  imessage:
    bluebubbles_url: http://bb.local
    bluebubbles_password: pw
    webhook_path_secret: s3cr3t-path
    unknown_sender: reply
  limits:
    max_text_chars: 20
    per_handle_per_minute: 20
"""

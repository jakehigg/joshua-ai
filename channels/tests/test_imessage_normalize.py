"""Guards + normalization. Pure functions, fixture-driven, no I/O.

Ported from the joshua/imessage bridge suite. The behaviour is unchanged; the
old ``to_event`` wire-shape assertions are dropped because Joshua builds a
``TurnEvent`` in the adapter instead of a multipart event.
"""

from __future__ import annotations

import pytest
from imessage_helpers import load_data, load_envelope
from joshua_channels.adapters.imessage.normalize import (
    DROP_FROM_ME,
    DROP_NO_CHAT,
    DROP_NO_CONTENT,
    DROP_TAPBACK,
    SKIP_NO_GUID,
    SKIP_TOO_LARGE,
    InboundMessage,
    Rejected,
    clean_text,
    is_group_guid,
    is_hidden_attachment,
    is_tapback,
    normalize_address,
    normalize_message,
    safe_filename,
    unwrap_webhook,
)

MAX_BYTES = 26214400


def norm(name: str, max_bytes: int = MAX_BYTES):
    return normalize_message(load_data(name), max_attachment_bytes=max_bytes)


# ── happy paths ──────────────────────────────────────────────────────────────


def test_dm_text():
    msg = norm("dm_text")
    assert isinstance(msg, InboundMessage)
    assert msg.message_guid == "AABBCCDD-1111-2222-3333-444455556666"
    assert msg.chat_guid == "iMessage;-;+15551234567"
    assert msg.is_group is False
    assert msg.sender_address == "+15551234567"  # phones verbatim
    assert msg.sender_service == "iMessage"
    assert msg.text == "hey"
    assert msg.chat_display_name is None
    assert msg.date_created == 1754300000000
    assert msg.attachments == ()


def test_group_text_lowercases_email_and_flags_group():
    msg = norm("group_text")
    assert isinstance(msg, InboundMessage)
    assert msg.is_group is True
    assert msg.chat_guid == "iMessage;+;chat100000000000000001"
    assert msg.sender_address == "mia.example@example.com"
    assert msg.chat_display_name == "Everyone"


def test_sms_service_handle():
    msg = norm("sms_handle")
    assert isinstance(msg, InboundMessage)
    assert msg.sender_service == "SMS"
    assert msg.sender_address == "+15559998888"
    assert msg.is_group is False


def test_attachment_message_survives_empty_text_and_marks_oversize():
    msg = norm("attachment")
    assert isinstance(msg, InboundMessage)
    # The text is only U+FFFC (the attachment placeholder) → "" on the wire.
    assert msg.text == ""
    assert len(msg.attachments) == 2

    first, second = msg.attachments
    assert (first.index, first.filename, first.mime_type, first.size) == (
        0,
        "IMG_0001.HEIC",
        "image/heic",
        123456,
    )
    assert first.skipped is False and first.skip_reason is None
    # 99999999 > 26214400 → skipped before any download is attempted.
    assert second.skipped is True and second.skip_reason == SKIP_TOO_LARGE


# ── hidden plugin-payload attachments (link previews) ────────────────────────


def test_url_balloon_keeps_its_text_and_omits_the_hidden_payload():
    """Messages.app sends "prose + link" as two messages; the second is a URL
    balloon whose text IS the url and which drags a ~20KB link-preview blob
    along. After the blob is omitted it is an ordinary text message."""
    msg = norm("url_balloon")
    assert isinstance(msg, InboundMessage)
    assert msg.text == "https://www.example.com/recipes/braised-short-ribs"
    assert msg.attachments == ()  # omitted entirely, not skipped


def test_hidden_attachment_predicate():
    assert is_hidden_attachment({"hideAttachment": True}) is True
    assert is_hidden_attachment({"hideAttachment": 1}) is True
    assert is_hidden_attachment({"transferName": "ABC.pluginPayloadAttachment"}) is True
    # Case-insensitive: BB has not promised the casing.
    assert is_hidden_attachment({"transferName": "abc.PLUGINPAYLOADATTACHMENT"}) is True
    assert is_hidden_attachment({"transferName": "IMG_0001.HEIC"}) is False
    assert is_hidden_attachment({}) is False


def test_omitted_attachments_do_not_consume_a_file_index():
    """``index`` binds attachments[i] to the downloaded blob — a hidden blob
    must not sit in the middle of that sequence."""
    data = load_data("attachment")
    hidden = {
        "guid": "att-guid-hidden",
        "transferName": "DEAD-BEEF.pluginPayloadAttachment",
        "mimeType": None,
        "totalBytes": 20480,
        "hideAttachment": True,
    }
    data["attachments"] = [hidden, *data["attachments"], dict(hidden)]
    msg = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(msg, InboundMessage)
    assert [a.index for a in msg.attachments] == [0, 1]
    assert [a.filename for a in msg.attachments] == ["IMG_0001.HEIC", "MOV_0002.MOV"]


def test_hidden_only_attachments_with_no_text_are_dropped_as_empty():
    """Omission is total: nothing is left, so guard 4 fires."""
    data = load_data("url_balloon")
    data["text"] = "￼"
    result = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(result, Rejected) and result.reason == DROP_NO_CONTENT


# ── guards, in order ─────────────────────────────────────────────────────────


def test_guard1_from_me_dropped_before_handle_is_read():
    # The fixture has handle: null — this must not blow up.
    result = norm("from_me_group")
    assert isinstance(result, Rejected)
    assert result.reason == DROP_FROM_ME


def test_guard2_tapback_dropped():
    result = norm("tapback")
    assert isinstance(result, Rejected)
    assert result.reason == DROP_TAPBACK


@pytest.mark.parametrize(
    "value,expected",
    [
        (2000, True),
        ("2000", True),  # BB has shipped both int and str
        (3001, True),
        ("sticker", True),  # non-numeric marker → still an association
        (0, False),
        ("0", False),
        (None, False),
        ("", False),
    ],
)
def test_associated_message_type_int_or_str(value, expected):
    assert is_tapback({"associatedMessageType": value}) is expected


def test_guard2_associated_guid_alone_is_enough():
    assert is_tapback({"associatedMessageGuid": "p:0/ABC", "associatedMessageType": None})


def test_guard3_missing_chats_dropped():
    result = norm("no_chats")
    assert isinstance(result, Rejected)
    assert result.reason == DROP_NO_CHAT


def test_guard3_chat_without_guid_dropped():
    data = load_data("dm_text")
    data["chats"] = [{"originalROWID": 3, "style": 45}]
    result = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(result, Rejected) and result.reason == DROP_NO_CHAT


def test_guard4_empty_text_no_attachments_dropped():
    data = load_data("dm_text")
    data["text"] = "   "
    result = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(result, Rejected) and result.reason == DROP_NO_CONTENT


def test_guard_order_from_me_wins_over_tapback():
    """A tapback WE sent is dropped as from_me — guard 1 runs first."""
    data = load_data("tapback")
    data["isFromMe"] = True
    result = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(result, Rejected) and result.reason == DROP_FROM_ME


# ── small pure helpers ───────────────────────────────────────────────────────


def test_is_group_guid():
    assert is_group_guid("iMessage;+;chat100000000000000001") is True
    assert is_group_guid("iMessage;-;+15551234567") is False
    assert is_group_guid("SMS;-;+15559998888") is False


def test_normalize_address():
    assert normalize_address("Alex.Smith@Example.COM") == "alex.smith@example.com"
    assert normalize_address("+15551234567") == "+15551234567"
    assert normalize_address(None) == ""


def test_clean_text_strips_object_replacement():
    assert clean_text("￼look at this￼") == "look at this"
    assert clean_text("￼") == ""
    assert clean_text(None) == ""


def test_safe_filename_is_a_basename():
    assert safe_filename("../../etc/passwd", 0) == "passwd"
    assert safe_filename("", 3) == "attachment-3"


def test_attachment_without_guid_is_skipped():
    data = load_data("attachment")
    data["attachments"] = [{"transferName": "x.png", "mimeType": "image/png", "totalBytes": 10}]
    msg = normalize_message(data, max_attachment_bytes=MAX_BYTES)
    assert isinstance(msg, InboundMessage)
    assert msg.attachments[0].skipped is True
    assert msg.attachments[0].skip_reason == SKIP_NO_GUID


def test_unwrap_webhook():
    assert unwrap_webhook(load_envelope("dm_text"))[0] == "new-message"
    assert unwrap_webhook({"type": "updated-message"})[1] == {}
    assert unwrap_webhook("garbage") == ("", {})


def test_normalize_never_raises_on_junk():
    for junk in [None, "", 5, [], {"guid": None, "chats": "nope"}]:
        assert isinstance(normalize_message(junk, max_attachment_bytes=MAX_BYTES), Rejected)

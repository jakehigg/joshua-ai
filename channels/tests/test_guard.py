"""Guard tests: allowlist, group fallback, rate limit, size caps, and audit."""

from __future__ import annotations

from joshua_channels.guard import (
    REASON_RATE_LIMITED,
    REASON_TRUNCATED,
    REASON_UNKNOWN,
    Guard,
)
from joshua_shared import config as config_module

GROUP_CHAT = "-100200"
OPEN_CHAT = "-100999"

CONFIG = f"""
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
groups:
  - id: everyone
    channel: telegram
    chat_id: "{GROUP_CHAT}"
    members:
      - "111222"
  - id: openchat
    channel: telegram
    chat_id: "{OPEN_CHAT}"
channels:
  limits:
    per_handle_per_minute: 20
    max_text_chars: 50
    max_attachment_bytes: 100
"""


def _settings(sidecar=None):
    return config_module.parse(CONFIG, env={}, source="<test>", sidecar_people=sidecar)


def _guard(settings=None, *, clock=None, config_provider=None) -> Guard:
    settings = settings or _settings()
    return Guard(
        settings,
        settings.channels.limits,
        config_provider=config_provider,
        clock=clock or (lambda: 1000.0),
    )


def _check(
    guard: Guard,
    handle: str,
    *,
    chat_id: str = "dm",
    chat_kind: str = "dm",
    text_len: int = 1,
    attachment_bytes: int = 0,
    chat_title: str | None = None,
):
    return guard.check(
        channel_type="telegram",
        sender_handle=handle,
        chat_id=chat_id,
        chat_kind=chat_kind,
        text_len=text_len,
        attachment_bytes=attachment_bytes,
        chat_title=chat_title,
    )


def test_known_dm_handle_is_allowed_with_person() -> None:
    verdict = _check(_guard(), "998877")
    assert verdict.allowed is True
    assert verdict.person_id == "alex"
    assert verdict.group_id is None
    assert verdict.reason is None


def test_unknown_dm_is_refused() -> None:
    verdict = _check(_guard(), "555")
    assert verdict.allowed is False
    assert verdict.reason == REASON_UNKNOWN
    assert verdict.person_id is None


def test_group_member_is_allowed_as_group() -> None:
    verdict = _check(_guard(), "111222", chat_id=GROUP_CHAT, chat_kind="group")
    assert verdict.allowed is True
    assert verdict.person_id is None
    assert verdict.group_id == "everyone"


def test_group_non_member_is_refused() -> None:
    verdict = _check(_guard(), "999000", chat_id=GROUP_CHAT, chat_kind="group")
    assert verdict.allowed is False
    assert verdict.reason == REASON_UNKNOWN


def test_group_without_members_admits_anyone() -> None:
    verdict = _check(_guard(), "777", chat_id=OPEN_CHAT, chat_kind="group")
    assert verdict.allowed is True
    assert verdict.group_id == "openchat"
    assert verdict.person_id is None


def test_unknown_sender_in_dm_never_falls_back_to_group() -> None:
    # A group chat_id in a DM context does not open the group path.
    verdict = _check(_guard(), "777", chat_id=OPEN_CHAT, chat_kind="dm")
    assert verdict.allowed is False
    assert verdict.reason == REASON_UNKNOWN


def test_rate_limit_refuses_the_twenty_first() -> None:
    guard = _guard()  # fixed clock: no refill within the minute
    for _ in range(20):
        assert _check(guard, "998877").allowed is True
    twenty_first = _check(guard, "998877")
    assert twenty_first.allowed is False
    assert twenty_first.reason == REASON_RATE_LIMITED


def test_rate_limit_is_per_handle() -> None:
    guard = _guard()
    for _ in range(20):
        _check(guard, "998877")
    # A different handle (a group member) has its own bucket.
    other = _check(guard, "111222", chat_id=GROUP_CHAT, chat_kind="group")
    assert other.allowed is True


def test_rate_bucket_refills_over_time() -> None:
    now = {"t": 1000.0}
    guard = _guard(clock=lambda: now["t"])
    for _ in range(20):
        _check(guard, "998877")
    assert _check(guard, "998877").allowed is False
    now["t"] += 6.0  # 6 s at 20/min refills two tokens
    assert _check(guard, "998877").allowed is True


def test_text_over_cap_is_truncated_but_allowed() -> None:
    verdict = _check(_guard(), "998877", text_len=51)
    assert verdict.allowed is True
    assert verdict.reason == REASON_TRUNCATED


def test_attachment_over_cap_keeps_the_message() -> None:
    verdict = _check(_guard(), "998877", text_len=1, attachment_bytes=500)
    assert verdict.allowed is True
    assert verdict.reason is None


def test_runtime_enrollment_is_honored_without_restart() -> None:
    base = _settings()
    enrolled = _settings(
        sidecar=[{"id": "sam", "name": "Sam", "role": "member", "handles": {"telegram": "555"}}]
    )
    current = {"cfg": base}
    guard = _guard(base, config_provider=lambda: current["cfg"])

    assert _check(guard, "555").allowed is False
    current["cfg"] = enrolled
    verdict = _check(guard, "555")
    assert verdict.allowed is True
    assert verdict.person_id == "sam"


def test_stats_count_refusals_and_truncations() -> None:
    guard = _guard()
    _check(guard, "555")  # unknown
    _check(guard, "999000", chat_id=GROUP_CHAT, chat_kind="group")  # unknown
    _check(guard, "998877", text_len=51)  # truncated, still allowed
    stats = guard.stats()
    assert stats[REASON_UNKNOWN] == 2
    assert stats[REASON_TRUNCATED] == 1
    assert stats[REASON_RATE_LIMITED] == 0
    assert stats["refused_total"] == 2


def test_recent_ring_records_the_address() -> None:
    guard = _guard()
    _check(guard, "555", chat_id="dm-chat")
    recent = guard.recent()
    assert len(recent) == 1
    entry = recent[0]
    assert entry["address"] == "555"
    assert entry["chat_id"] == "dm-chat"
    assert entry["reason"] == REASON_UNKNOWN
    assert entry["channel_type"] == "telegram"


def test_recent_ring_is_bounded() -> None:
    from joshua_channels.guard import RECENT_RING_SIZE

    guard = _guard()
    for i in range(RECENT_RING_SIZE + 10):
        _check(guard, f"h{i}")
    assert len(guard.recent()) == RECENT_RING_SIZE


def test_a_refusal_is_logged(caplog) -> None:
    """A dropped message is silent to the sender, so it must reach the log.

    `unknown_sender: drop` sends nothing back, so the log and the audit are the
    only places an operator sees the attempt.
    """
    with caplog.at_level("WARNING"):
        _check(_guard(), "555")
    records = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    refusals = [r for r in records if r.get("message") == "inbound refused"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == REASON_UNKNOWN
    assert refusals[0]["address"] == "555"
    assert refusals[0]["channel_type"] == "telegram"


def test_a_rate_limit_refusal_is_logged(caplog) -> None:
    guard = _guard()
    for _ in range(20):
        _check(guard, "998877")
    with caplog.at_level("WARNING"):
        _check(guard, "998877")
    reasons = [
        r.msg["reason"]
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "inbound refused"
    ]
    assert reasons == [REASON_RATE_LIMITED]


def test_an_allowed_message_logs_no_refusal(caplog) -> None:
    with caplog.at_level("WARNING"):
        _check(_guard(), "998877")
    assert not [r for r in caplog.records if isinstance(r.msg, dict) and "refused" in str(r.msg)]


def test_the_refusal_log_never_holds_the_message(caplog) -> None:
    """The guard sees only a length, never the text. This pins that."""
    with caplog.at_level("WARNING"):
        _check(_guard(), "555", text_len=4000)
    for record in caplog.records:
        assert "text" not in (record.msg if isinstance(record.msg, dict) else {})


# --- Unconfigured groups -----------------------------------------------------
#
# An operator cannot add a group whose chat id never appears anywhere. The
# refusal ring does not hold it: a sender on the roster is allowed, so no
# refusal happens and nothing is recorded.

NEW_CHAT = "-100777"


async def test_enrolled_sender_in_an_unconfigured_group_is_recorded() -> None:
    guard = _guard()
    verdict = _check(guard, "998877", chat_id=NEW_CHAT, chat_kind="group", chat_title="Family")
    assert verdict.allowed  # alex is on the roster, so no refusal is recorded
    assert guard.recent() == []
    groups = guard.unconfigured()
    assert len(groups) == 1
    assert groups[0]["chat_id"] == NEW_CHAT
    assert groups[0]["chat_title"] == "Family"
    assert groups[0]["channel_type"] == "telegram"


async def test_unknown_sender_in_an_unconfigured_group_is_recorded_once() -> None:
    guard = _guard()
    refused = _check(guard, "000000", chat_id=NEW_CHAT, chat_kind="group")
    assert not refused.allowed and refused.reason == REASON_UNKNOWN
    _check(guard, "998877", chat_id=NEW_CHAT, chat_kind="group")
    assert len(guard.unconfigured()) == 1


async def test_a_configured_group_is_not_recorded() -> None:
    guard = _guard()
    _check(guard, "111222", chat_id=GROUP_CHAT, chat_kind="group")
    _check(guard, "998877", chat_id=OPEN_CHAT, chat_kind="group")
    assert guard.unconfigured() == []


async def test_a_direct_message_is_not_recorded() -> None:
    guard = _guard()
    _check(guard, "998877", chat_id="dm", chat_kind="dm")
    assert guard.unconfigured() == []


async def test_the_record_holds_no_text_and_no_handle() -> None:
    """The chat is named, the people in it are not."""
    guard = _guard()
    _check(guard, "998877", chat_id=NEW_CHAT, chat_kind="group", chat_title="Family")
    entry = guard.unconfigured()[0]
    assert set(entry) == {"channel_type", "chat_id", "chat_title", "at"}
    assert "998877" not in str(entry)


async def test_the_ring_is_bounded() -> None:
    from joshua_channels.guard import UNCONFIGURED_RING_SIZE

    guard = _guard()
    for n in range(UNCONFIGURED_RING_SIZE + 5):
        _check(guard, "998877", chat_id=f"-100{n:04d}", chat_kind="group")
    assert len(guard.unconfigured()) == UNCONFIGURED_RING_SIZE

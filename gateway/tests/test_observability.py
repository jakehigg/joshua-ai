"""Unit tests for the call log and caller registry."""

from __future__ import annotations

from joshua_gateway.observability import (
    CallerRegistry,
    CallLog,
    SessionRegistry,
    call_log_size,
)


def test_call_log_size_default_and_override(monkeypatch):
    monkeypatch.delenv("GATEWAY_CALL_LOG_SIZE", raising=False)
    assert call_log_size() == 2000
    monkeypatch.setenv("GATEWAY_CALL_LOG_SIZE", "5")
    assert call_log_size() == 5
    monkeypatch.setenv("GATEWAY_CALL_LOG_SIZE", "bad")
    assert call_log_size() == 2000


def test_call_log_records_newest_first_and_filters():
    log = CallLog(maxlen=10)
    log.record({"identity": "core", "tool": "echo", "status": "success"})
    log.record({"identity": "laptop", "tool": "secret_tool", "status": "denied"})
    recent = log.recent()
    assert recent[0]["tool"] == "secret_tool"
    assert [c["tool"] for c in log.recent(identity="core")] == ["echo"]
    assert [c["tool"] for c in log.recent(tool="secret")] == ["secret_tool"]
    assert log.recent(limit=1) == recent[:1]


def test_call_log_evicts_oldest():
    log = CallLog(maxlen=2)
    for i in range(3):
        log.record({"identity": "core", "tool": f"t{i}", "status": "success"})
    assert [c["tool"] for c in log.recent()] == ["t2", "t1"]


def test_caller_registry_tracks_touch_and_calls():
    registry = CallerRegistry()
    registry.touch("core", "echo")
    registry.record_call("core")
    snapshot = registry.snapshot()
    assert snapshot["core"]["calls"] == 1
    assert "echo" in snapshot["core"]["servers"]


def test_session_registry_binds_then_detects_conflicts():
    sessions = SessionRegistry()
    assert sessions.check("a", "s1", "alex", "c1") is None
    # The same person and conversation is consistent.
    assert sessions.check("a", "s1", "alex", "c1") is None
    # A later request with no person or conversation keeps the binding.
    assert sessions.check("a", "s1", None, None) is None
    assert sessions.check("a", "s1", "mia", None) == "person_changed"
    assert sessions.check("a", "s1", None, "c2") == "conversation_changed"


def test_session_registry_close_reopens_key():
    sessions = SessionRegistry()
    sessions.check("a", "s1", "alex", None)
    sessions.close("a", "s1")
    # After close the key rebinds to a new person with no conflict.
    assert sessions.check("a", "s1", "mia", None) is None


def test_session_registry_live_persons_and_clear():
    sessions = SessionRegistry()
    sessions.check("a", "s1", "alex", None)
    sessions.check("a", "s2", "mia", None)
    sessions.check("a", "s3", None, None)  # no person: not listed
    sessions.check("b", "s4", "alex", None)
    assert sessions.live_persons("a") == ["alex", "mia"]
    sessions.clear_server("a")
    assert sessions.live_persons("a") == []
    assert sessions.live_persons("b") == ["alex"]

"""Resolution tests: names, group/dm refs, literal ids, and unknown targets."""

from __future__ import annotations

from adapter_fakes import FakeAdapter
from joshua_channels import destinations
from joshua_channels.registry import Adapter, AdapterRegistry
from joshua_shared import config as config_module

GROUP_CHAT = "iMessage;+;chat100000000000000001"

CONFIG = f"""
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      imessage: "+15551234567"
groups:
  - id: everyone
    channel: imessage
    chat_id: "{GROUP_CHAT}"
channels:
  destinations:
    everyone: imessage:group:everyone
    alexdm: imessage:dm:alex
"""


def _settings():
    return config_module.parse(CONFIG, env={}, source="<test>")


def _registry() -> tuple[AdapterRegistry, FakeAdapter]:
    registry = AdapterRegistry()
    adapter = FakeAdapter("imessage", {"group:everyone": GROUP_CHAT, "dm:alex": "+15551234567"})
    registry.register(adapter)
    return registry, adapter


def test_registry_holds_and_reports_adapters() -> None:
    registry, adapter = _registry()
    assert isinstance(adapter, Adapter)
    assert registry.get("imessage") is adapter
    assert registry.get("telegram") is None
    assert registry.types() == ["imessage"]
    assert "imessage" in registry


async def test_registry_starts_and_stops_all() -> None:
    registry, adapter = _registry()
    await registry.start_all()
    assert adapter.started is True
    await registry.stop_all()
    assert adapter.stopped is True


async def test_registry_stop_survives_a_failing_adapter() -> None:
    class _BadStop(FakeAdapter):
        async def stop(self) -> None:
            raise RuntimeError("stop boom")

    registry = AdapterRegistry()
    registry.register(_BadStop("telegram"))
    good = FakeAdapter("imessage")
    registry.register(good)
    await registry.stop_all()  # must not raise
    assert good.stopped is True


def test_name_resolves_to_group_chat_id() -> None:
    registry, _ = _registry()
    resolved = destinations.resolve(registry, _settings(), "everyone")
    assert resolved is not None
    assert resolved.chat_id == GROUP_CHAT
    assert resolved.channel == f"imessage:{GROUP_CHAT}"
    assert resolved.kind == "group"
    assert resolved.person_id is None
    assert resolved.title == "everyone"


def test_name_resolves_to_dm() -> None:
    registry, _ = _registry()
    resolved = destinations.resolve(registry, _settings(), "alexdm")
    assert resolved is not None
    assert resolved.kind == "dm"
    assert resolved.chat_id == "+15551234567"
    assert resolved.person_id == "alex"
    assert resolved.title == "Alex"


def test_group_ref_resolves() -> None:
    registry, _ = _registry()
    resolved = destinations.resolve(registry, _settings(), "imessage:group:everyone")
    assert resolved is not None
    assert resolved.chat_id == GROUP_CHAT
    assert resolved.kind == "group"


def test_literal_group_chat_id_is_a_group() -> None:
    registry, _ = _registry()
    resolved = destinations.resolve(registry, _settings(), f"imessage:{GROUP_CHAT}")
    assert resolved is not None
    assert resolved.kind == "group"
    assert resolved.chat_id == GROUP_CHAT


def test_literal_unknown_chat_id_is_a_dm() -> None:
    registry, _ = _registry()
    resolved = destinations.resolve(registry, _settings(), "imessage:+15559999999")
    assert resolved is not None
    assert resolved.kind == "dm"
    assert resolved.person_id is None


def test_unknown_name_is_none() -> None:
    registry, _ = _registry()
    assert destinations.resolve(registry, _settings(), "nope") is None


def test_unknown_adapter_type_is_none() -> None:
    registry, _ = _registry()
    assert destinations.resolve(registry, _settings(), "telegram:123") is None


def test_unresolvable_ref_is_none() -> None:
    registry, _ = _registry()
    # The adapter has no dm entry for this person.
    assert destinations.resolve(registry, _settings(), "imessage:dm:ghost") is None


def test_empty_target_is_none() -> None:
    registry, _ = _registry()
    assert destinations.resolve(registry, _settings(), "  ") is None

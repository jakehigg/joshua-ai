"""Backend selection: ``resolve_agent_backend`` maps ``AGENT_BACKEND`` to the
session backend the manager runs. Offline; no DB, SDK, or network."""

from __future__ import annotations

import pytest
from joshua_core.main import resolve_agent_backend


def test_default_is_sdk() -> None:
    assert resolve_agent_backend({}) == "sdk"


def test_empty_value_is_sdk() -> None:
    assert resolve_agent_backend({"AGENT_BACKEND": ""}) == "sdk"


def test_stub_selected() -> None:
    assert resolve_agent_backend({"AGENT_BACKEND": "stub"}) == "stub"


def test_value_is_normalized() -> None:
    assert resolve_agent_backend({"AGENT_BACKEND": " SDK "}) == "sdk"


def test_unknown_value_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="AGENT_BACKEND"):
        resolve_agent_backend({"AGENT_BACKEND": "gpt"})

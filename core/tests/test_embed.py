"""Offline tests for the embedding module — no model download.

Only the guarded branches run: empty text short-circuits before any model load,
and a forced-unavailable model makes ``embed``/``warmup`` degrade gracefully.
"""

from __future__ import annotations

import pytest
from joshua_core.memory import embed as embed_module


def test_to_pgvector_formats_literal() -> None:
    assert embed_module.to_pgvector([1.0, 2.5, -3.0]) == "[1.0,2.5,-3.0]"


def test_embed_empty_text_returns_none() -> None:
    assert embed_module.embed("") is None
    assert embed_module.embed("   ") is None


def test_embed_unavailable_model_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "_unavailable", True)
    monkeypatch.setattr(embed_module, "_model", None)
    assert embed_module.embed("some real text") is None
    assert embed_module.warmup() is False

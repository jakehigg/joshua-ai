"""Offline tests for the embedding module — no model download.

Only the guarded branches run: empty text short-circuits before any model load,
a forced-unavailable model makes ``embed``/``warmup`` degrade gracefully, and
the cache directory tests use a stub in place of fastembed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

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


def test_usable_cache_dir_none_stays_none() -> None:
    assert embed_module._usable_cache_dir(None) is None
    assert embed_module._usable_cache_dir("") is None


def test_usable_cache_dir_keeps_a_writable_dir(tmp_path: Path) -> None:
    target = tmp_path / "cache"
    assert embed_module._usable_cache_dir(str(target)) == str(target)
    assert target.is_dir()


def test_usable_cache_dir_leaves_no_probe(tmp_path: Path) -> None:
    target = tmp_path / "cache"
    embed_module._usable_cache_dir(str(target))
    assert list(target.iterdir()) == []


def test_usable_cache_dir_falls_back_when_it_cannot_write(tmp_path: Path) -> None:
    """A path below a regular file can never be made, whatever the uid is.

    A read-only mode would not prove this: root ignores the mode bits, and CI
    can run as root.
    """
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    got = embed_module._usable_cache_dir(str(blocker / "cache"))
    assert got == os.path.join(tempfile.gettempdir(), "joshua-fastembed")


def test_get_model_passes_the_fallback_to_fastembed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unwritable configured dir must not reach fastembed."""
    seen: dict[str, object] = {}

    class _StubTextEmbedding:
        def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
            seen["model_name"] = model_name
            seen["cache_dir"] = cache_dir

    stub = types.ModuleType("fastembed")
    stub.TextEmbedding = _StubTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", stub)
    monkeypatch.setattr(embed_module, "_model", None)
    monkeypatch.setattr(embed_module, "_unavailable", False)

    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    monkeypatch.setenv("MEMORY_EMBED_CACHE_DIR", str(blocker / "cache"))

    embed_module._get_model("BAAI/bge-small-en-v1.5")
    assert seen["cache_dir"] == os.path.join(tempfile.gettempdir(), "joshua-fastembed")


def test_is_available_reports_without_a_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "_unavailable", False)
    monkeypatch.setattr(embed_module, "_model", None)
    assert embed_module.is_available() is True
    monkeypatch.setattr(embed_module, "_unavailable", True)
    assert embed_module.is_available() is False

"""The `knowledge/` and `insights/` paths in `joshua_shared.layout`, and the
values they refuse."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_shared import layout


def test_roots(tmp_path: Path) -> None:
    assert layout.knowledge_root(tmp_path) == tmp_path / "wiki" / "knowledge"
    assert layout.insights_root(tmp_path) == tmp_path / "wiki" / "insights"


def test_roots_follow_the_data_dir_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(tmp_path))
    assert layout.knowledge_root() == tmp_path / "wiki" / "knowledge"
    assert layout.insights_root() == tmp_path / "wiki" / "insights"


def test_knowledge_dossier_mirrors_the_topic_path(tmp_path: Path) -> None:
    got = layout.knowledge_dossier("houseplants/snake-plant.md", tmp_path)
    assert got == tmp_path / "wiki" / "knowledge" / "houseplants" / "snake-plant.md"


def test_knowledge_dossier_of_a_root_page(tmp_path: Path) -> None:
    assert layout.knowledge_dossier("garden.md", tmp_path) == (
        tmp_path / "wiki" / "knowledge" / "garden.md"
    )


def test_knowledge_dossier_accepts_a_path_without_md(tmp_path: Path) -> None:
    assert layout.knowledge_dossier("houseplants/snake-plant", tmp_path) == (
        layout.knowledge_dossier("houseplants/snake-plant.md", tmp_path)
    )


def test_knowledge_page(tmp_path: Path) -> None:
    got = layout.knowledge_page("houseplants/snake-plant.md", "winter-watering", tmp_path)
    assert got == (
        tmp_path / "wiki" / "knowledge" / "houseplants" / "snake-plant" / "winter-watering.md"
    )


def test_insight_page(tmp_path: Path) -> None:
    got = layout.insight_page("garden", "autumn-watering-too-high", tmp_path)
    assert got == tmp_path / "wiki" / "insights" / "garden" / "autumn-watering-too-high.md"


BAD_TOPICS = [
    "",
    "/etc/passwd.md",
    "../escape.md",
    "houseplants/../../escape.md",
    "./houseplants/snake-plant.md",
    "houseplants//snake-plant.md",
    "houseplants/.hidden.md",
    ".git/config",
    "houseplants\\snake-plant.md",
    "houseplants/snake\0plant.md",
    "houseplants/",
]


@pytest.mark.parametrize("topic", BAD_TOPICS)
def test_bad_topic_path_is_refused(topic: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        layout.knowledge_dossier(topic, tmp_path)
    with pytest.raises(ValueError):
        layout.knowledge_page(topic, "ok", tmp_path)


def test_topic_path_must_be_text(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        layout.knowledge_dossier(None, tmp_path)  # type: ignore[arg-type]


BAD_SLUGS = ["", "Winter", "-lead", "a/b", "..", "a.md", "a_b", "x" * 65, "a b"]


@pytest.mark.parametrize("slug", BAD_SLUGS)
def test_bad_slug_is_refused(slug: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        layout.knowledge_page("houseplants/snake-plant.md", slug, tmp_path)
    with pytest.raises(ValueError):
        layout.insight_page("garden", slug, tmp_path)
    with pytest.raises(ValueError):
        layout.insight_page(slug, "ok", tmp_path)


def test_bootstrap_wiki_does_not_make_the_job_folders(tmp_path: Path) -> None:
    layout.bootstrap_wiki(tmp_path)
    assert not layout.knowledge_root(tmp_path).exists()
    assert not layout.insights_root(tmp_path).exists()

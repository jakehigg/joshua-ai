"""Moving the pictures that wiki pages already point at into the wiki."""

from __future__ import annotations

from pathlib import Path

from joshua_core.migrate_links import migrate_page_links
from joshua_shared.attachments import AttachmentMeta, read_meta, write_meta

JPEG = b"\xff\xd8\xff"


def _evidence(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    write_meta(path, AttachmentMeta(mime="image/jpeg", original_name="plant.jpg"))
    return path


def _page(root: Path, rel: str, text: str) -> Path:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_page_keeps_its_picture(tmp_path: Path) -> None:
    """Retention deletes the evidence. The page must not lose the picture."""
    _evidence(tmp_path, "people/alex/attachments/2026/08/plant.jpg")
    page = _page(
        tmp_path,
        "plants/snake.md",
        "# Snake plant\n\n![the plant](people/alex/attachments/2026/08/plant.jpg)\n",
    )

    counts = migrate_page_links(tmp_path)

    assert counts == {"pages": 1, "files": 1}
    assert "(/attachments/2026/08/plant.jpg)" in page.read_text()
    copied = tmp_path / "wiki" / "attachments" / "2026" / "08" / "plant.jpg"
    assert copied.read_bytes() == JPEG
    # The evidence is never moved.
    assert (tmp_path / "people/alex/attachments/2026/08/plant.jpg").is_file()


def test_the_copy_says_where_it_came_from(tmp_path: Path) -> None:
    _evidence(tmp_path, "people/alex/attachments/2026/08/plant.jpg")
    _page(tmp_path, "p.md", "![x](people/alex/attachments/2026/08/plant.jpg)\n")

    migrate_page_links(tmp_path)

    meta = read_meta(tmp_path / "wiki/attachments/2026/08/plant.jpg")
    assert meta is not None
    assert meta.saved_from == "people/alex/attachments/2026/08/plant.jpg"
    assert meta.original_name == "plant.jpg"


def test_a_shared_attachment_moves_too(tmp_path: Path) -> None:
    _evidence(tmp_path, "shared/attachments/everyone/2026/08/team.jpg")
    page = _page(tmp_path, "p.md", "![x](/shared/attachments/everyone/2026/08/team.jpg)\n")

    migrate_page_links(tmp_path)

    assert "(/attachments/2026/08/team.jpg)" in page.read_text()


def test_running_it_again_changes_nothing(tmp_path: Path) -> None:
    _evidence(tmp_path, "people/alex/attachments/2026/08/plant.jpg")
    page = _page(tmp_path, "p.md", "![x](people/alex/attachments/2026/08/plant.jpg)\n")

    migrate_page_links(tmp_path)
    first = page.read_text()
    counts = migrate_page_links(tmp_path)

    assert counts == {"pages": 0, "files": 0}
    assert page.read_text() == first
    assert len(list((tmp_path / "wiki" / "attachments").rglob("*.jpg"))) == 1


def test_two_pages_that_use_one_picture_share_the_copy(tmp_path: Path) -> None:
    _evidence(tmp_path, "people/alex/attachments/2026/08/plant.jpg")
    one = _page(tmp_path, "a.md", "![x](people/alex/attachments/2026/08/plant.jpg)\n")
    two = _page(tmp_path, "b.md", "![y](people/alex/attachments/2026/08/plant.jpg)\n")

    counts = migrate_page_links(tmp_path)

    assert counts == {"pages": 2, "files": 1}
    assert "(/attachments/2026/08/plant.jpg)" in one.read_text()
    assert "(/attachments/2026/08/plant.jpg)" in two.read_text()


def test_a_link_to_a_file_that_is_gone_is_left_alone(tmp_path: Path) -> None:
    """Retention already took it. The page keeps the link it has."""
    page = _page(tmp_path, "p.md", "![x](people/alex/attachments/2026/08/gone.jpg)\n")
    before = page.read_text()

    counts = migrate_page_links(tmp_path)

    assert counts == {"pages": 0, "files": 0}
    assert page.read_text() == before


def test_a_page_with_no_such_link_is_untouched(tmp_path: Path) -> None:
    page = _page(tmp_path, "p.md", "# Plain\n\nNo pictures here.\n")
    before = page.read_text()

    migrate_page_links(tmp_path)

    assert page.read_text() == before


def test_a_link_already_in_the_wiki_is_left_alone(tmp_path: Path) -> None:
    page = _page(tmp_path, "p.md", "![x](/attachments/2026/08/plant.jpg)\n")
    before = page.read_text()

    migrate_page_links(tmp_path)

    assert page.read_text() == before


def test_a_link_cannot_reach_outside_the_volume(tmp_path: Path) -> None:
    """A page that names a path above the volume moves nothing."""
    secret = tmp_path.parent / "secret.jpg"
    secret.write_bytes(b"secret")
    page = _page(tmp_path, "p.md", "![x](people/alex/attachments/../../../secret.jpg)\n")
    before = page.read_text()

    counts = migrate_page_links(tmp_path)

    assert counts == {"pages": 0, "files": 0}
    assert page.read_text() == before
    assert not list((tmp_path / "wiki").rglob("secret.jpg"))


def test_a_hidden_page_is_not_read(tmp_path: Path) -> None:
    _evidence(tmp_path, "people/alex/attachments/2026/08/plant.jpg")
    trashed = _page(tmp_path, ".trash/old.md", "![x](people/alex/attachments/2026/08/plant.jpg)\n")
    before = trashed.read_text()

    migrate_page_links(tmp_path)

    assert trashed.read_text() == before


def test_no_wiki_is_not_a_failure(tmp_path: Path) -> None:
    assert migrate_page_links(tmp_path) == {"pages": 0, "files": 0}

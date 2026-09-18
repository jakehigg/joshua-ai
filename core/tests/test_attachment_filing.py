"""Naming an attachment, and moving one into the wiki."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.attachments import file_attachment, stored_name
from joshua_shared.attachments import AttachmentMeta, Description, read_meta, write_meta

RECEIPT = Description(kind="receipt", subject="a grocery receipt", slug="grocery-receipt")


def _stored(root: Path, rel: str, *, description: Description | None = RECEIPT) -> AttachmentMeta:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff")
    meta = AttachmentMeta(
        mime="image/jpeg",
        original_name="IMG_4471.jpg",
        received_at="2026-09-16T14:05:09+00:00",
        sha256="a" * 64,
        sent_by="alex",
        description=description,
    )
    write_meta(path, meta)
    return meta


@pytest.mark.parametrize(
    ("current", "slug", "expected"),
    [
        (
            "2026-09-16-140509-IMG_4471.jpg",
            "grocery-receipt",
            "2026-09-16-140509-grocery-receipt.jpg",
        ),
        ("2026-09-16-140509-x.pdf", "water-bill", "2026-09-16-140509-water-bill.pdf"),
        ("photo.jpg", "snake-plant", "snake-plant.jpg"),
    ],
)
def test_the_name_keeps_the_timestamp_and_takes_the_slug(
    current: str, slug: str, expected: str
) -> None:
    assert stored_name(current, slug) == expected


def test_a_file_is_renamed_where_it_is_when_auto_save_is_off(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=False, is_member=True)

    assert new_rel == "people/alex/attachments/2026/09/2026-09-16-140509-grocery-receipt.jpg"
    assert (tmp_path / new_rel).is_file()
    assert not (tmp_path / rel).exists()
    assert read_meta(tmp_path / new_rel) is not None
    # The old metadata file goes with the old name.
    assert not (tmp_path / f"{rel}.meta.json").exists()


def test_a_member_file_moves_into_the_wiki_when_auto_save_is_on(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, moved = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == "wiki/attachments/2026/09/2026-09-16-140509-grocery-receipt.jpg"
    assert (tmp_path / new_rel).is_file()
    assert not (tmp_path / rel).exists()
    assert moved.saved_from == rel
    assert read_meta(tmp_path / new_rel) is not None


def test_a_guest_file_never_moves_into_the_wiki(tmp_path: Path) -> None:
    """A guest does not write the wiki, so a guest's file stays where it is."""
    rel = "people/sam/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=False)

    assert new_rel.startswith("people/sam/attachments/")
    assert not list((tmp_path / "wiki").rglob("*.jpg"))


def test_a_group_file_moves_into_the_wiki_too(tmp_path: Path) -> None:
    rel = "shared/attachments/everyone/2026/09/2026-09-16-140509-IMG.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == "wiki/attachments/2026/09/2026-09-16-140509-grocery-receipt.jpg"


def test_a_file_with_no_description_is_left_alone(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel, description=None)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == rel
    assert (tmp_path / rel).is_file()


def test_a_second_file_with_the_same_name_keeps_both(tmp_path: Path) -> None:
    first = "people/alex/attachments/2026/09/2026-09-16-140509-a.jpg"
    second = "people/alex/attachments/2026/09/2026-09-16-140509-b.jpg"
    meta_a = _stored(tmp_path, first)
    meta_b = _stored(tmp_path, second)

    one, _ = file_attachment(first, meta_a, data_root=tmp_path, auto_save=True, is_member=True)
    two, _ = file_attachment(second, meta_b, data_root=tmp_path, auto_save=True, is_member=True)

    assert one != two
    assert (tmp_path / one).is_file()
    assert (tmp_path / two).is_file()


def test_a_file_already_in_the_wiki_is_not_moved_again(tmp_path: Path) -> None:
    rel = "wiki/attachments/2026/09/2026-09-16-140509-grocery-receipt.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == rel
    assert (tmp_path / rel).is_file()


def test_a_missing_file_leaves_the_path_alone(tmp_path: Path) -> None:
    meta = AttachmentMeta(mime="image/jpeg", description=RECEIPT)
    rel = "people/alex/attachments/2026/09/gone.jpg"

    assert file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)[0] == rel


@pytest.mark.parametrize(
    "rel",
    [
        "people/alex/cli/outbox.jsonl",
        "wiki/recipes/pizza.md",
        "../../etc/passwd",
    ],
)
def test_a_path_that_is_not_an_attachment_is_never_moved(tmp_path: Path, rel: str) -> None:
    """Only a file in an attachment tree is filed. Nothing else is touched."""
    meta = AttachmentMeta(mime="image/jpeg", description=RECEIPT)

    assert file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)[0] == rel

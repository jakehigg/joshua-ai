"""Naming an attachment, and moving one into the wiki."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.attachments import file_attachment, stored_name
from joshua_shared.attachments import AttachmentMeta, Description, read_meta, write_meta

RECEIPT = Description(kind="receipt", subject="a grocery receipt", slug="grocery-receipt")


def _stored(
    root: Path,
    rel: str,
    *,
    description: Description | None = RECEIPT,
    sha256: str = "a" * 64,
    body: bytes = b"\xff\xd8\xff",
) -> AttachmentMeta:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    meta = AttachmentMeta(
        mime="image/jpeg",
        original_name="IMG_4471.jpg",
        received_at="2026-09-16T14:05:09+00:00",
        sha256=sha256,
        sent_by="alex",
        description=description,
    )
    write_meta(path, meta)
    return meta


@pytest.mark.parametrize(
    ("current", "slug", "sha256", "expected"),
    [
        (
            "2026-09-16-140509-IMG_4471.jpg",
            "grocery-receipt",
            "d7e12240" + "0" * 56,
            "grocery-receipt-d7e122.jpg",
        ),
        ("2026-09-16-140509-x.pdf", "water-bill", "ab" * 32, "water-bill-ababab.pdf"),
        ("photo.jpg", "snake-plant", None, "snake-plant.jpg"),
    ],
)
def test_the_name_is_the_slug_and_the_digest(
    current: str, slug: str, sha256: str | None, expected: str
) -> None:
    """The folder carries the month, so the name carries no date."""
    assert stored_name(current, slug, sha256) == expected


def test_a_file_is_renamed_where_it_is_when_auto_save_is_off(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=False, is_member=True)

    assert new_rel == "people/alex/attachments/2026/09/grocery-receipt-aaaaaa.jpg"
    assert (tmp_path / new_rel).is_file()
    assert not (tmp_path / rel).exists()
    assert read_meta(tmp_path / new_rel) is not None
    # The old metadata file goes with the old name.
    assert not (tmp_path / f"{rel}.meta.json").exists()


def test_a_member_file_moves_into_the_wiki_when_auto_save_is_on(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel)

    new_rel, moved = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == "wiki/attachments/2026/09/grocery-receipt-aaaaaa.jpg"
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

    assert new_rel == "wiki/attachments/2026/09/grocery-receipt-aaaaaa.jpg"


def test_a_file_with_no_description_is_left_alone(tmp_path: Path) -> None:
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG_4471.jpg"
    meta = _stored(tmp_path, rel, description=None)

    new_rel, _ = file_attachment(rel, meta, data_root=tmp_path, auto_save=True, is_member=True)

    assert new_rel == rel
    assert (tmp_path / rel).is_file()


def test_two_different_files_of_one_second_both_survive(tmp_path: Path) -> None:
    """Same slug, same second, different bytes. Neither one may be lost."""
    first = "people/alex/attachments/2026/09/2026-09-16-140509-a.jpg"
    second = "people/alex/attachments/2026/09/2026-09-16-140509-b.jpg"
    meta_a = _stored(tmp_path, first, sha256="a" * 64, body=b"\xff\xd8\xffA")
    meta_b = _stored(tmp_path, second, sha256="b" * 64, body=b"\xff\xd8\xffB")

    one, _ = file_attachment(first, meta_a, data_root=tmp_path, auto_save=True, is_member=True)
    two, _ = file_attachment(second, meta_b, data_root=tmp_path, auto_save=True, is_member=True)

    assert one != two
    assert (tmp_path / one).read_bytes() == b"\xff\xd8\xffA"
    assert (tmp_path / two).read_bytes() == b"\xff\xd8\xffB"
    # The digest of each file is in its own name.
    assert one.endswith("-aaaaaa.jpg")
    assert two.endswith("-bbbbbb.jpg")


def test_the_same_file_twice_makes_one_copy(tmp_path: Path) -> None:
    """The name is the bytes, so a file filed twice is one file, not two."""
    first = "people/alex/attachments/2026/09/2026-09-16-140509-a.jpg"
    second = "people/mia/attachments/2026/09/2026-09-16-140509-b.jpg"
    meta_a = _stored(tmp_path, first)
    meta_b = _stored(tmp_path, second)

    one, _ = file_attachment(first, meta_a, data_root=tmp_path, auto_save=True, is_member=True)
    two, _ = file_attachment(second, meta_b, data_root=tmp_path, auto_save=True, is_member=True)

    assert one == two
    assert len(list((tmp_path / "wiki" / "attachments").rglob("*.jpg"))) == 1


def test_a_clash_of_two_different_files_never_overwrites(tmp_path: Path) -> None:
    """A name taken by other bytes gets a counter, whoever wrote it first."""
    from joshua_core.attachments import _free_path

    folder = tmp_path / "wiki" / "attachments" / "2026" / "09"
    folder.mkdir(parents=True)
    taken = folder / "grocery-receipt-aaaaaa.jpg"
    taken.write_bytes(b"first")
    write_meta(taken, AttachmentMeta(mime="image/jpeg", sha256="a" * 64))

    same = _free_path(folder, taken.name, "a" * 64)
    other = _free_path(folder, taken.name, "z" * 64)
    unknown = _free_path(folder, taken.name, None)

    assert same == taken
    assert other != taken
    assert unknown != taken
    assert taken.read_bytes() == b"first"


def test_a_file_already_in_the_wiki_is_not_moved_again(tmp_path: Path) -> None:
    rel = "wiki/attachments/2026/09/grocery-receipt-aaaaaa.jpg"
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

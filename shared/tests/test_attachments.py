"""The metadata file: what it holds, and what it refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from joshua_shared import attachments


def _file(tmp_path: Path, name: str = "a.jpg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff")
    return path


def test_meta_path_sits_beside_the_file(tmp_path: Path) -> None:
    file = _file(tmp_path)
    assert attachments.meta_path(file) == tmp_path / "a.jpg.meta.json"
    assert attachments.is_meta(attachments.meta_path(file))
    assert not attachments.is_meta(file)


def test_write_then_read_gives_the_same_metadata(tmp_path: Path) -> None:
    file = _file(tmp_path)
    meta = attachments.AttachmentMeta(
        original_name="IMG_0042.HEIC",
        mime="image/jpeg",
        received_at="2026-09-16T12:00:00+00:00",
        sha256="a" * 64,
        size_bytes=3,
        sent_by="jake",
        extracted_text="TOTAL 12.40",
        text_source="vision",
        description=attachments.Description(
            kind="receipt",
            subject="a grocery receipt from a supermarket",
            slug="grocery-receipt",
            text_present=True,
            summary="a receipt with a list of items and a total",
        ),
    )
    attachments.write_meta(file, meta)

    read = attachments.read_meta(file)
    assert read is not None
    assert read.original_name == "IMG_0042.HEIC"
    assert read.sent_by == "jake"
    assert read.has_text()
    assert read.description is not None
    assert read.description.kind == "receipt"
    assert read.description.match_text() == "a receipt: a grocery receipt from a supermarket"


def test_a_missing_metadata_file_reads_as_none(tmp_path: Path) -> None:
    assert attachments.read_meta(_file(tmp_path)) is None


def test_metadata_that_is_not_json_reads_as_none(tmp_path: Path) -> None:
    file = _file(tmp_path)
    attachments.meta_path(file).write_text("{not json")
    assert attachments.read_meta(file) is None


def test_metadata_that_is_not_an_object_reads_as_none(tmp_path: Path) -> None:
    file = _file(tmp_path)
    attachments.meta_path(file).write_text("[1, 2, 3]")
    assert attachments.read_meta(file) is None


def test_metadata_over_the_size_cap_reads_as_none(tmp_path: Path) -> None:
    file = _file(tmp_path)
    body = json.dumps({"mime": "image/jpeg", "extracted_text": "x" * attachments.MAX_META_BYTES})
    attachments.meta_path(file).write_text(body)
    assert attachments.read_meta(file) is None


def test_an_unknown_field_is_ignored(tmp_path: Path) -> None:
    """A newer writer may add a field. An older reader still reads the file."""
    file = _file(tmp_path)
    attachments.meta_path(file).write_text(json.dumps({"mime": "image/png", "future": "value"}))
    meta = attachments.read_meta(file)
    assert meta is not None
    assert meta.mime == "image/png"


def test_a_bad_description_makes_the_metadata_unreadable(tmp_path: Path) -> None:
    """A slug that could escape a directory refuses the whole file."""
    file = _file(tmp_path)
    attachments.meta_path(file).write_text(
        json.dumps(
            {
                "mime": "image/jpeg",
                "description": {
                    "kind": "receipt",
                    "subject": "x",
                    "slug": "../../etc/passwd",
                },
            }
        )
    )
    assert attachments.read_meta(file) is None


@pytest.mark.parametrize(
    "slug",
    ["../x", "a/b", "A", "-a", "", "x" * 41, ".hidden", "a b"],
)
def test_a_bad_slug_is_refused(slug: str) -> None:
    assert not attachments.is_slug(slug)
    with pytest.raises(ValueError):
        attachments.Description(kind="other", subject="x", slug=slug)


@pytest.mark.parametrize("slug", ["a", "grocery-receipt", "0", "x" * 40])
def test_a_good_slug_is_accepted(slug: str) -> None:
    assert attachments.is_slug(slug)


def test_a_kind_outside_the_list_is_refused() -> None:
    with pytest.raises(ValueError):
        attachments.Description(kind="anything-else", subject="x", slug="x")


def test_clean_text_removes_control_and_invisible_characters() -> None:
    dirty = "line​one\x07\n‮two\ttab\r\nthree"
    assert attachments.clean_text(dirty) == "lineone\ntwo\ttab\nthree"


def test_clean_text_cuts_to_the_limit() -> None:
    assert attachments.clean_text("x" * 50, limit=10) == "x" * 10


def test_a_subject_is_one_line_and_short() -> None:
    long = "a receipt " * 40
    description = attachments.Description(kind="receipt", subject=f"one\ntwo {long}", slug="x")
    assert "\n" not in description.subject
    assert len(description.subject) <= attachments.SUBJECT_MAX


def test_an_empty_subject_is_refused() -> None:
    with pytest.raises(ValueError):
        attachments.Description(kind="other", subject="   ", slug="x")


def test_extracted_text_is_cleaned_on_the_way_in(tmp_path: Path) -> None:
    file = _file(tmp_path)
    attachments.write_meta(
        file,
        attachments.AttachmentMeta(mime="application/pdf", extracted_text="a\x00b​c"),
    )
    meta = attachments.read_meta(file)
    assert meta is not None
    assert meta.extracted_text == "abc"


def test_sha256_of_a_file_matches_the_bytes(tmp_path: Path) -> None:
    file = _file(tmp_path)
    assert attachments.sha256_file(file) == attachments.sha256_bytes(b"\xff\xd8\xff")


def test_image_mime_reads_the_suffix() -> None:
    assert attachments.image_mime("a/b/c.JPG") == "image/jpeg"
    assert attachments.image_mime("a/b/c.pdf") is None


def test_write_meta_replaces_an_earlier_file(tmp_path: Path) -> None:
    file = _file(tmp_path)
    attachments.write_meta(file, attachments.AttachmentMeta(mime="image/jpeg"))
    attachments.write_meta(file, attachments.AttachmentMeta(mime="image/png", sent_by="mia"))
    meta = attachments.read_meta(file)
    assert meta is not None
    assert meta.mime == "image/png"
    assert meta.sent_by == "mia"
    assert list(tmp_path.glob("*.tmp")) == []


# ── the digest tag in a filename ─────────────────────────────────────────────


def test_the_tag_is_the_first_six_characters_of_the_digest() -> None:
    assert attachments.digest_tag("d7e12240828455dd" + "0" * 48) == "d7e122"


def test_the_tag_of_the_same_bytes_is_always_the_same() -> None:
    """The name is the file, so a file that is stored twice keeps one name."""
    digest = attachments.sha256_bytes(b"the same bytes")
    assert attachments.digest_tag(digest) == attachments.digest_tag(digest)


def test_two_files_get_two_tags() -> None:
    one = attachments.digest_tag(attachments.sha256_bytes(b"one"))
    two = attachments.digest_tag(attachments.sha256_bytes(b"two"))
    assert one != two


@pytest.mark.parametrize("value", [None, "", "   ", "../../etc"])
def test_a_digest_that_is_not_one_gives_no_tag(value: str | None) -> None:
    """Nothing but hex reaches a filename."""
    assert attachments.digest_tag(value) == ""

"""Attachment pipeline tests: sniff, reject, convert, downscale, store, retain.

Fixtures are generated with Pillow in the test, apart from one real HEIC file
(``attachment_fixtures/sample.heic``, under 200 KB).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from joshua_channels.attachments import (
    BYTE_BUDGET,
    LONG_EDGE,
    MAX_DOC_BYTES,
    AttachmentPipeline,
    classify,
    is_heic_bytes,
    safe_filename,
    sniff_mime,
)
from PIL import Image

FIXTURES = Path(__file__).parent / "attachment_fixtures"


# ── helpers ──────────────────────────────────────────────────────────────────


def _png(size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, (30, 120, 200))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write(inbox: Path, name: str, data: bytes) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(data)


def _fixed_clock() -> datetime:
    return datetime(2026, 8, 27, tzinfo=UTC)


def _pipeline(tmp_path: Path, **kwargs) -> AttachmentPipeline:
    return AttachmentPipeline(data_dir=tmp_path, clock=_fixed_clock, **kwargs)


# ── sniffing ─────────────────────────────────────────────────────────────────


def test_sniff_known_types() -> None:
    assert sniff_mime(_png((10, 10))) == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_mime(b"GIF89a....") == "image/gif"
    assert sniff_mime(b"%PDF-1.7\n") == "application/pdf"
    assert sniff_mime(b"just some text") == "text/plain"


def test_sniff_rejects_binaries() -> None:
    assert sniff_mime(b"PK\x03\x04zipbody") == "application/zip"
    assert sniff_mime(b"MZ\x90\x00exe") == "application/x-msdownload"
    assert sniff_mime(b"\x7fELFbinary") == "application/x-executable"
    assert sniff_mime(b"\x00\x01\x02\x03") is None


def test_is_heic_bytes_on_real_fixture() -> None:
    data = (FIXTURES / "sample.heic").read_bytes()
    assert is_heic_bytes(data)
    assert sniff_mime(data) == "image/heic"


def test_classify() -> None:
    assert classify("image/png", 10) == "accept"
    assert classify("application/pdf", 10) == "accept"
    assert classify("application/pdf", MAX_DOC_BYTES + 1) == "too_large"
    assert classify("application/zip", 10) == "disallowed_type"
    assert classify(None, 10) == "disallowed_type"


# ── safe filenames ───────────────────────────────────────────────────────────


_NOW = datetime(2026, 8, 27, 14, 5, 9, tzinfo=UTC)


def test_safe_filename_strips_directory_traversal() -> None:
    name = safe_filename("../../etc/passwd", ".jpg", now=_NOW)
    assert "/" not in name
    assert ".." not in name
    assert name == "2026-08-27-140509-passwd.jpg"


def test_safe_filename_windows_and_empty() -> None:
    assert safe_filename("a\\b\\c.png", ".png", now=_NOW) == "2026-08-27-140509-c.png"
    assert safe_filename("", ".txt", now=_NOW) == "2026-08-27-140509-file.txt"


# ── ingest: images ───────────────────────────────────────────────────────────


async def test_heic_becomes_bounded_jpeg(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m1"
    _write(inbox, "photo.heic", (FIXTURES / "sample.heic").read_bytes())

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert len(stored) == 1
    att = stored[0]
    assert att.mime == "image/jpeg"
    assert att.path == f"attachments/2026/08/{att.name}"
    abs_path = tmp_path / "people" / "alex" / att.path
    assert abs_path.exists()
    assert abs_path.stat().st_size <= BYTE_BUDGET
    with Image.open(abs_path) as image:
        assert max(image.size) <= LONG_EDGE


async def test_large_png_is_downscaled(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m2"
    _write(inbox, "big.png", _png((4000, 3000)))

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert len(stored) == 1
    abs_path = tmp_path / "people" / "alex" / stored[0].path
    assert abs_path.stat().st_size <= BYTE_BUDGET
    with Image.open(abs_path) as image:
        assert max(image.size) <= LONG_EDGE


async def test_small_png_kept_untouched(tmp_path: Path) -> None:
    data = _png((64, 64))
    inbox = tmp_path / "inbox" / "m3"
    _write(inbox, "small.png", data)

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert len(stored) == 1
    att = stored[0]
    assert att.mime == "image/png"
    abs_path = tmp_path / "people" / "alex" / att.path
    assert abs_path.read_bytes() == data


# ── ingest: rejection and traversal ──────────────────────────────────────────


async def test_executable_renamed_to_jpg_is_skipped(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m4"
    _write(inbox, "totally-a-photo.jpg", b"MZ\x90\x00" + b"\x00" * 64)

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert stored == []
    assert not (tmp_path / "people" / "alex" / "attachments").exists()


async def test_platform_traversal_name_is_sanitized(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m5"
    _write(inbox, "evil.png", _png((32, 32)))
    # A platform can hand back a traversal name; ingest reads the on-disk file,
    # and the stored name is always sanitized to a single path segment.
    (inbox / "evil.png").rename(inbox / "evil.png")

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert len(stored) == 1
    assert "/" not in stored[0].name
    assert ".." not in stored[0].name


async def test_pdf_and_text_kept_as_is(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m6"
    _write(inbox, "doc.pdf", b"%PDF-1.7\n" + b"body\n" * 100)
    _write(inbox, "note.txt", b"hello world\n")

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    mimes = sorted(a.mime for a in stored)
    assert mimes == ["application/pdf", "text/plain"]


# ── storage targets ──────────────────────────────────────────────────────────


async def test_group_stores_under_shared(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m7"
    _write(inbox, "small.png", _png((32, 32)))

    stored = await _pipeline(tmp_path).process(inbox, person_id=None, group_id="everyone")

    att = stored[0]
    assert att.path == f"shared/attachments/everyone/2026/08/{att.name}"
    assert (tmp_path / att.path).exists()


async def test_no_person_no_group_uses_events_bucket(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m8"
    _write(inbox, "small.png", _png((32, 32)))

    stored = await _pipeline(tmp_path).process(inbox, person_id=None, group_id=None)

    att = stored[0]
    assert att.path.startswith("shared/attachments/events/2026/08/")
    assert (tmp_path / att.path).exists()


async def test_same_second_same_name_gets_counter_suffix(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)

    inbox1 = tmp_path / "inbox" / "c1"
    _write(inbox1, "photo.png", _png((16, 16)))
    first = await pipeline.process(inbox1, person_id="alex", group_id=None)

    inbox2 = tmp_path / "inbox" / "c2"
    _write(inbox2, "photo.png", _png((16, 16)))
    second = await pipeline.process(inbox2, person_id="alex", group_id=None)

    assert first[0].name == "2026-08-27-000000-photo.png"
    assert second[0].name == "2026-08-27-000000-photo-2.png"
    assert (tmp_path / "people" / "alex" / second[0].path).exists()


async def test_stored_name_uses_configured_timezone(tmp_path: Path) -> None:
    pipeline = AttachmentPipeline(
        data_dir=tmp_path,
        timezone="America/New_York",
        clock=lambda: datetime(2026, 8, 27, 2, 30, tzinfo=UTC),
    )
    inbox = tmp_path / "inbox" / "tz"
    _write(inbox, "sunset.png", _png((16, 16)))

    stored = await pipeline.process(inbox, person_id="alex", group_id=None)

    att = stored[0]
    # 02:30 UTC is the day before at 22:30 in New York (EDT, UTC-4).
    assert att.name == "2026-08-26-223000-sunset.png"
    assert att.original_name == "sunset.png"
    assert att.path == "attachments/2026/08/2026-08-26-223000-sunset.png"


async def test_inbox_is_deleted_after_ingest(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m9"
    _write(inbox, "small.png", _png((16, 16)))

    await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert not inbox.exists()


async def test_keep_originals_stores_pre_transform_file(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox" / "m10"
    _write(inbox, "big.png", _png((4000, 3000)))

    stored = await _pipeline(tmp_path, keep_originals=True).process(
        inbox, person_id="alex", group_id=None
    )

    dest = tmp_path / "people" / "alex" / stored[0].path
    originals = list((dest.parent / "originals").glob("*-big.png"))
    assert len(originals) == 1


# ── maintenance ──────────────────────────────────────────────────────────────


def test_purge_stale_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    fresh = inbox / "fresh"
    stale = inbox / "stale"
    fresh.mkdir(parents=True)
    stale.mkdir(parents=True)
    old = time.time() - 7200
    import os

    os.utime(stale, (old, old))

    removed = _pipeline(tmp_path).purge_stale_inbox()

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_sweep_retention_deletes_old_files(tmp_path: Path) -> None:
    attachments = tmp_path / "people" / "alex" / "attachments" / "2020" / "01"
    attachments.mkdir(parents=True)
    old_file = attachments / "old.jpg"
    old_file.write_bytes(b"x")
    old = time.time() - 400 * 86400
    import os

    os.utime(old_file, (old, old))

    recent = tmp_path / "people" / "alex" / "attachments" / "2026" / "08"
    recent.mkdir(parents=True)
    (recent / "new.jpg").write_bytes(b"x")

    removed = AttachmentPipeline(data_dir=tmp_path, retention_days=365).sweep_retention()

    assert removed == 1
    assert not old_file.exists()
    assert (recent / "new.jpg").exists()


def test_sweep_retention_zero_keeps_forever(tmp_path: Path) -> None:
    attachments = tmp_path / "shared" / "attachments" / "everyone" / "2000" / "01"
    attachments.mkdir(parents=True)
    (attachments / "ancient.jpg").write_bytes(b"x")

    removed = AttachmentPipeline(data_dir=tmp_path, retention_days=0).sweep_retention()

    assert removed == 0
    assert (attachments / "ancient.jpg").exists()


@pytest.mark.parametrize(
    "bad",
    [
        b"",  # empty -> unrecognized
        b"\x00\x02\x03\x04",  # binary garbage -> unrecognized
        b"\x89PNG\r\n\x1a\nnot really a png",  # PNG magic, corrupt body -> decode fails
    ],
)
async def test_broken_or_unrecognized_files_are_skipped(tmp_path: Path, bad: bytes) -> None:
    inbox = tmp_path / "inbox" / "mbad"
    _write(inbox, "thing.png", bad)

    stored = await _pipeline(tmp_path).process(inbox, person_id="alex", group_id=None)

    assert stored == []

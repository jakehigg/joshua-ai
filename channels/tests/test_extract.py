"""Text extraction from a stored attachment.

A PDF is untrusted input. These tests prove that a broken or hostile file gives
no text and never raises into the ingest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from joshua_channels import extract
from pdf_fixtures import pdf_with_text


def test_a_text_file_gives_its_text(tmp_path: Path) -> None:
    data = b"hello\nworld\n"
    found = extract.extract(data, "text/plain", tmp_path / "a.txt", max_chars=1000)
    assert found is not None
    assert found.text == "hello\nworld\n"
    assert found.source == "text"
    assert not found.truncated


def test_a_text_file_is_cleaned_and_cut(tmp_path: Path) -> None:
    data = ("a\x07b​c" + "x" * 100).encode()
    found = extract.extract(data, "text/plain", tmp_path / "a.txt", max_chars=10)
    assert found is not None
    assert found.text.startswith("abc")
    assert len(found.text) == 10
    assert found.truncated


def test_an_empty_text_file_gives_nothing(tmp_path: Path) -> None:
    assert extract.extract(b"   \n", "text/plain", tmp_path / "a.txt", max_chars=100) is None


def test_an_image_gives_nothing(tmp_path: Path) -> None:
    """A picture is the describer's work, not this module's."""
    assert extract.extract(b"\xff\xd8\xff", "image/jpeg", tmp_path / "a.jpg", max_chars=100) is None


def test_a_pdf_with_a_text_layer_gives_its_text(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(
        pdf_with_text(["Water bill account 12345", "Call 555-0100 before the due date"])
    )

    found = extract.extract(path.read_bytes(), "application/pdf", path, max_chars=10_000)

    assert found is not None
    assert found.source == "pdf"
    assert "Water bill" in found.text
    assert "555-0100" in found.text
    assert found.pages == 1


def test_a_pdf_with_no_text_layer_gives_nothing(tmp_path: Path) -> None:
    """A scan holds no text. The describer in core reads that one."""
    from pypdf import PdfWriter

    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)

    assert extract.extract(path.read_bytes(), "application/pdf", path, max_chars=1000) is None


def test_a_broken_pdf_gives_nothing_and_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nnot really a pdf\n")
    assert extract.extract(path.read_bytes(), "application/pdf", path, max_chars=1000) is None


def test_a_pdf_that_runs_too_long_is_given_up_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser that never finishes must not hold up the turn."""
    path = tmp_path / "slow.pdf"
    path.write_bytes(b"%PDF-1.7\n")

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="pdf_text", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert extract.extract(path.read_bytes(), "application/pdf", path, max_chars=1000) is None


def test_the_pdf_reader_runs_in_another_process(tmp_path: Path) -> None:
    """The parse is a subprocess, so a crash never reaches channels."""
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def record(cmd: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    import joshua_channels.extract as module

    original = module.subprocess.run
    module.subprocess.run = record  # type: ignore[assignment]
    try:
        module.extract(path.read_bytes(), "application/pdf", path, max_chars=100)
    finally:
        module.subprocess.run = original  # type: ignore[assignment]

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[1:3] == ["-m", "joshua_channels.pdf_text"]
    assert seen["timeout"] == extract.PDF_TIMEOUT_S


def test_the_reader_subprocess_caps_its_own_memory_and_cpu() -> None:
    """The child limits itself, so one file cannot take the machine."""
    from joshua_channels import pdf_text

    assert pdf_text.MEMORY_LIMIT_BYTES <= 1024 * 1024 * 1024
    assert pdf_text.CPU_LIMIT_SECONDS <= 60


def test_the_reader_refuses_a_call_with_bad_arguments() -> None:
    from joshua_channels import pdf_text

    assert pdf_text.main(["pdf_text"]) == 2
    assert pdf_text.main(["pdf_text", "/nope.pdf", "5", "100"]) == 1

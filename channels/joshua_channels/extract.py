"""Text extraction for a stored attachment.

`channels` is the inbound trust boundary, so the parse of an untrusted file
happens here and not in `core`. The text goes in the metadata file, which makes
it available to the agent, to a search, and to the memory index, and the file
is parsed one time only.

Two kinds of file give text here:

- A PDF with a text layer. `pdf_text.py` reads it in a process of its own, with
  a memory limit, a CPU limit, and a timeout.
- A text file. It is decoded as UTF-8 and cleaned.

A PDF with no text layer (a scan, a photo of a page) gives no text here. The
describer in `core` reads that one.

The text is data, never an instruction. It reaches a prompt wrapped as
untrusted content, and the memory index marks it as external.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from joshua_shared.attachments import TextSource, clean_text
from joshua_shared.log import get_logger

logger = get_logger("channels.extract")

# The pages of a PDF this reads, and how long it waits for the subprocess.
PDF_MAX_PAGES = 50
PDF_TIMEOUT_S = 25.0

# A PDF that gives less text than this for each page it has is a scan. The
# describer in core looks at it instead.
MIN_CHARS_PER_PAGE = 16


@dataclass(frozen=True)
class Extracted:
    """The text of one attachment, and where it came from."""

    text: str
    source: TextSource
    pages: int | None = None
    truncated: bool = False


def extract(data: bytes, mime: str, path: Path, *, max_chars: int) -> Extracted | None:
    """Return the text of an attachment, or `None` when there is none to read.

    `data` is the stored bytes and `path` is where they are. A failure of any
    kind returns `None`: text is useful, and never worth failing an attachment
    that is already stored.
    """
    try:
        if mime == "text/plain":
            return _from_text(data, max_chars)
        if mime == "application/pdf":
            return _from_pdf(path, max_chars)
    except Exception as exc:  # noqa: BLE001 - extraction never fails an attachment
        logger.warning({"message": "attachment text extraction failed", "error": str(exc)})
    return None


def _from_text(data: bytes, max_chars: int) -> Extracted | None:
    text = clean_text(data.decode("utf-8", errors="replace"), limit=max_chars)
    if not text.strip():
        return None
    return Extracted(text=text, source="text", truncated=len(data) > max_chars)


def _from_pdf(path: Path, max_chars: int) -> Extracted | None:
    """Read the text layer of a PDF in a subprocess. `None` when there is none."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "joshua_channels.pdf_text",
                str(path),
                str(PDF_MAX_PAGES),
                str(max_chars),
            ],
            capture_output=True,
            timeout=PDF_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning({"message": "pdf text extraction timed out", "name": path.name})
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    try:
        payload = json.loads(result.stdout)
        text = clean_text(str(payload.get("text") or ""), limit=max_chars)
        pages = int(payload.get("pages") or 0)
        truncated = bool(payload.get("truncated"))
    except (ValueError, TypeError):
        return None

    if not text.strip() or _looks_scanned(text, pages):
        return None
    return Extracted(text=text, source="pdf", pages=pages or None, truncated=truncated)


def _looks_scanned(text: str, pages: int) -> bool:
    """True when a PDF holds too little text for its page count to be a text layer."""
    read_pages = min(pages, PDF_MAX_PAGES) or 1
    return len(text.strip()) < MIN_CHARS_PER_PAGE * read_pages

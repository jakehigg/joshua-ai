"""The metadata file of an attachment, and the rules every container reads it by.

A stored attachment has a metadata file beside it: `<file>.meta.json`. Three
containers use it:

- `channels` writes it when the file arrives: the sender's filename, the
  sniffed MIME, the arrival time, the `sha256` of the stored bytes, and the text
  that it extracted from a PDF or a text file.
- `core` adds the description of the Haiku worker, and the text that the
  worker read from an image or a scanned PDF. `core` never writes the
  attachment itself.
- `gateway` and the memory index read it.

The file content is untrusted. It holds a filename that a sender chose and text
that came out of a document. `read_meta` parses it with `json` and validates it
with `AttachmentMeta`, and a file that is too large or not valid gives `None`.
Every reader uses `read_meta`, so the three containers apply one rule.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

# The text a message carries when it holds a file and no words of its own.
# `channels` sends it and `core` reads it, so the two sides must agree.
NO_CAPTION_TEXT = "(file attached — no caption)"


def is_no_caption(text: str) -> bool:
    """True when `text` is the placeholder and holds nothing a person wrote.

    A turn like this says nothing about the file, so a match against it means
    nothing. The describer gives the turn real words; until then, a skill match
    and a memory search on this text are skipped.
    """
    return text.strip() == NO_CAPTION_TEXT


# The suffix of the metadata file: `<stored-name>.meta.json`.
META_SUFFIX = ".meta.json"

# A metadata file larger than this is not read. The extracted text is capped
# well below it, so only a damaged or hostile file reaches the limit.
MAX_META_BYTES = 2 * 1024 * 1024

# The extracted text of one attachment is at most this many characters.
MAX_TEXT_CHARS = 200_000

# The image types that the agent and the describer can see. `channels` converts
# HEIC to JPEG, and it keeps BMP as BMP, which no model reads.
IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
VISION_MIMES = frozenset(IMAGE_MIME.values())

# What a description may call a file. The list is closed, so a value from the
# model cannot put new text into a prompt through this field.
Kind = Literal[
    "receipt",
    "bill",
    "invoice",
    "statement",
    "letter",
    "form",
    "document",
    "school-notice",
    "ticket",
    "screenshot",
    "handwriting",
    "whiteboard",
    "label",
    "menu",
    "recipe",
    "photo",
    "plant",
    "food",
    "animal",
    "person",
    "place",
    "object",
    "diagram",
    "other",
]
KINDS: tuple[str, ...] = get_args(Kind)

# Where the extracted text came from: `pdf` is the text layer of a PDF, `text`
# is a text file, `vision` is the Haiku worker reading an image or a scanned PDF.
TextSource = Literal["pdf", "text", "vision"]

# A slug names a file. It is checked here and again where a path is built.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\Z")

SUBJECT_MAX = 80
SUMMARY_MAX = 400

# A control character other than a newline or a tab, and the invisible format
# characters (zero width, bidirectional overrides) that can hide text from a
# reader. `clean_text` removes them.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Return `text` with control and invisible format characters removed.

    Keeps newlines and tabs. Normalizes line ends to `\\n` and cuts the result
    to `limit` characters.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text[:limit]


def one_line(text: str, limit: int) -> str:
    """Return `text` cleaned, on one line, with spaces collapsed, cut to `limit`.

    A long value is cut, never refused: a wordy answer from a model must not
    fail the description that holds it.
    """
    return " ".join(clean_text(text[: limit * 8], limit=limit * 4).split())[:limit]


def is_slug(value: str) -> bool:
    """True when `value` is a valid file slug."""
    return bool(SLUG_RE.match(value))


class _Model(BaseModel):
    # A newer writer can add a field. An older reader ignores it.
    model_config = ConfigDict(extra="ignore")


class Description(_Model):
    """What the Haiku worker says about one attachment.

    `subject`, `slug`, and `summary` describe the file. They never copy the
    text in it. The text goes in `AttachmentMeta.extracted_text`.
    """

    kind: Kind
    subject: str
    slug: str
    text_present: bool = False
    summary: str = ""
    model: str | None = None
    described_at: str | None = None

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        cleaned = one_line(value, SUBJECT_MAX)
        if not cleaned:
            raise ValueError("subject is empty")
        return cleaned

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return one_line(value, SUMMARY_MAX)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not is_slug(value):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]{0,39}$")
        return value

    def match_text(self) -> str:
        """The text that stands for the file in a skill match and a search."""
        return f"a {self.kind.replace('-', ' ')}: {self.subject}"


class AttachmentMeta(_Model):
    """The content of one metadata file."""

    original_name: str | None = None
    mime: str = "application/octet-stream"
    received_at: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    # The person who sent the file, when there is one.
    sent_by: str | None = None
    # The attachment path this file was copied or moved from, relative to the
    # data volume. Set for a file in `wiki/attachments/`.
    saved_from: str | None = None
    extracted_text: str | None = None
    text_source: TextSource | None = None
    text_truncated: bool = False
    pages: int | None = None
    # True while the worker still reads the rest of a long scanned PDF.
    transcript_pending: bool = False
    description: Description | None = None

    @field_validator("extracted_text")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = clean_text(value)
        return cleaned or None

    @field_validator("original_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return one_line(value, 255) or None

    def has_text(self) -> bool:
        return bool(self.extracted_text and self.extracted_text.strip())


# How much of the digest goes in a filename. Six hex characters are 16 million
# values, which is far more than one month folder ever holds, and they keep the
# name readable.
DIGEST_TAG_CHARS = 6


def digest_tag(sha256: str | None) -> str:
    """The short tag of a digest for a filename, or `""` when there is none.

    The tag is the file itself, not a random value. The same bytes always give
    the same name, so a file that is filed twice lands on one name and the
    second write is the same bytes. A random tag would make a second copy.
    """
    if not sha256:
        return ""
    tag = sha256.strip().lower()[:DIGEST_TAG_CHARS]
    return tag if tag.isalnum() else ""


def meta_path(file: Path) -> Path:
    """The metadata file of `file`: `<file>.meta.json` in the same directory."""
    return file.parent / f"{file.name}{META_SUFFIX}"


def is_meta(path: Path | str) -> bool:
    """True when `path` names a metadata file."""
    return str(path).endswith(META_SUFFIX)


def read_meta(file: Path) -> AttachmentMeta | None:
    """Return the metadata of `file`, or `None` when it is absent or not valid.

    A metadata file larger than `MAX_META_BYTES`, one that is not JSON, and one
    that does not match `AttachmentMeta` all give `None`. The function never
    raises for bad content.
    """
    path = meta_path(file)
    try:
        if not path.is_file() or path.stat().st_size > MAX_META_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return AttachmentMeta.model_validate(raw)
    except ValueError:
        return None


def write_meta(file: Path, meta: AttachmentMeta) -> Path:
    """Write the metadata of `file` atomically. Returns the metadata path."""
    path = meta_path(file)
    data = meta.model_dump_json(exclude_none=True, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_mime(path: Path | str) -> str | None:
    """The image MIME for the suffix of `path`, or `None` for another type."""
    return IMAGE_MIME.get(Path(path).suffix.lower())

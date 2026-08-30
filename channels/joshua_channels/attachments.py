"""The attachment pipeline: sniff, normalize, and store one inbound file.

Every adapter downloads its files into an inbox directory and hands the
directory to ``AttachmentPipeline.process``. The pipeline runs the same steps
for every file, whatever adapter it came from:

1. Sniff the MIME from magic bytes. The extension and the platform-declared
   type are not trusted.
2. Reject anything that is not an image, a PDF, or text. An executable or an
   archive is skipped with ``reason=disallowed_type``.
3. Convert HEIC/HEIF to JPEG.
4. Downscale an image to the agent bound: long edge ``1568`` px and at most
   ``600_000`` bytes. A file already inside the bound keeps its bytes.
5. Keep a PDF or text file as is when it is at most 25 MB.
6. Build a timestamped filename in the configured timezone, then store the file
   under ``/data/people/<person_id>/attachments/YYYY/MM/`` (a DM) or
   ``/data/shared/attachments/<group_id>/YYYY/MM/`` (a group).

The returned ``Attachment.path`` is files-MCP relative, so core passes paths to
the agent and never touches bytes. The inbox directory is deleted after ingest.

Retention: ``sweep_retention`` deletes stored files older than
``channels.limits.attachment_retention_days`` (``0`` keeps them forever), and
``purge_stale_inbox`` deletes inbox scratch older than one hour. The app runs
the inbox purge at startup and the retention sweep once a day.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from joshua_shared.contracts import Attachment
from joshua_shared.log import get_logger

logger = get_logger("channels.attachments")

# The agent-safe image bound: long edge in pixels and a byte budget. The stored
# image fits both; the byte budget keeps token cost down.
LONG_EDGE = 1568
BYTE_BUDGET = 600_000

# JPEG quality for a HEIC conversion, and the ladder a downscale walks down until
# the encoded image fits the byte budget.
JPEG_QUALITY = 90
QUALITY_FLOOR = 60
QUALITY_STEP = 5

# A PDF or text file is kept as is up to this size.
MAX_DOC_BYTES = 25 * 1024 * 1024

# Inbox scratch older than this is purged at startup.
INBOX_MAX_AGE_S = 3600

# How often the retention sweep runs.
RETENTION_INTERVAL_S = 86400

# The sidecar suffix a stored attachment carries: ``<stored-name>.meta.json``.
# The files MCP reads it for the sender's filename, so the two sides must agree.
META_SUFFIX = ".meta.json"

# Skip reasons (stable strings — they show up in logs and tests).
SKIP_DISALLOWED = "disallowed_type"
SKIP_TOO_LARGE = "too_large"
SKIP_UNREADABLE = "unreadable"

# HEIF/HEIC ``ftyp`` brands. The presence of any of these in the box marks the
# file as HEIC, whatever its extension claims.
_HEIF_BRANDS = frozenset(
    {
        b"heic",
        b"heix",
        b"heim",
        b"heis",
        b"hevc",
        b"hevx",
        b"hevm",
        b"hevs",
        b"heif",
        b"mif1",
        b"msf1",
    }
)

# Recognized disallowed signatures. Detection is not load-bearing — anything not
# explicitly allowed is dropped — but a named type gives a precise skip log.
_DISALLOWED_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
    (b"\x7fELF", "application/x-executable"),
    (b"MZ", "application/x-msdownload"),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary"),
    (b"\xce\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\xca\xfe\xba\xbe", "application/x-mach-binary"),
    (b"\x1f\x8b", "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
)

# MIME -> stored extension.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}

# Characters allowed in a stored filename stem. Everything else becomes ``_``.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

try:  # pragma: no cover - import guard
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001 - HEIC support is optional at import time
    logger.warning({"message": "pillow-heif not available; HEIC conversion disabled"})


# ── MIME sniffing (pure) ─────────────────────────────────────────────────────


def is_heic_bytes(data: bytes) -> bool:
    """True when the ISO-BMFF ``ftyp`` box carries a HEIF/HEIC brand."""
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    brands = {data[8:12]}
    window = data[16:64]
    for offset in range(0, len(window) - 3, 4):
        brands.add(window[offset : offset + 4])
    return bool(brands & _HEIF_BRANDS)


def looks_textual(data: bytes) -> bool:
    """True when the bytes decode as UTF-8 and hold no NUL byte."""
    if not data or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniff_mime(data: bytes) -> str | None:
    """Return the MIME sniffed from magic bytes, or None when unrecognized.

    Trusts only the bytes. A recognized disallowed type (archive, executable)
    returns its MIME so the caller can log a precise reason; the caller drops it.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if is_heic_bytes(data):
        return "image/heic"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    for signature, mime in _DISALLOWED_SIGNATURES:
        if data[: len(signature)] == signature:
            return mime
    if looks_textual(data):
        return "text/plain"
    return None


def classify(mime: str | None, size: int) -> str:
    """Return ``"accept"`` or a skip reason for a sniffed MIME and byte size."""
    if mime is None:
        return SKIP_DISALLOWED
    if mime.startswith("image/"):
        return "accept"
    if mime in ("application/pdf", "text/plain"):
        return "accept" if size <= MAX_DOC_BYTES else SKIP_TOO_LARGE
    return SKIP_DISALLOWED


def safe_filename(raw: str, ext: str, *, now: datetime) -> str:
    """Return a stored filename: a local-time stamp, a sanitized stem, and ``ext``.

    The name is ``YYYY-MM-DD-HHMMSS-<stem><ext>``. ``now`` is the arrival time in
    the configured timezone, so the name reads in the person's wall clock; the
    frontmatter and the index keep UTC. Strips any directory part, so a platform
    filename like ``../../x.jpg`` cannot escape the target directory.
    """
    base = posixpath.basename(str(raw).replace("\\", "/")).strip()
    stem = Path(base).stem
    stem = _UNSAFE_CHARS.sub("_", stem)[:48].strip("._")
    if not stem:
        stem = "file"
    return f"{now:%Y-%m-%d-%H%M%S}-{stem}{ext}"


def _dedupe_name(directory: Path, name: str, ext: str) -> str:
    """Return ``name``, or add ``-2``, ``-3``, … before ``ext`` on a clash.

    The timestamp already separates most files. Two files with the same original
    name in the same second collide, so the counter keeps them apart.
    """
    if not (directory / name).exists():
        return name
    stem = name[: -len(ext)] if ext and name.endswith(ext) else name
    counter = 2
    while (directory / f"{stem}-{counter}{ext}").exists():
        counter += 1
    return f"{stem}-{counter}{ext}"


# ── image transforms ─────────────────────────────────────────────────────────


def _encode_jpeg(image: object, quality: int) -> bytes:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)  # type: ignore[attr-defined]
    return buffer.getvalue()


def _encode_jpeg_to_budget(image: object) -> bytes:
    """Encode as JPEG, stepping quality down until the result fits the budget."""
    encoded = b""
    for quality in range(JPEG_QUALITY, QUALITY_FLOOR - 1, -QUALITY_STEP):
        encoded = _encode_jpeg(image, quality)
        if len(encoded) <= BYTE_BUDGET:
            return encoded
    return encoded  # best effort at the quality floor


def _fit_long_edge(image: object):  # type: ignore[no-untyped-def]
    """Resize so the long edge is at most ``LONG_EDGE``. Smaller images pass."""
    from PIL import Image

    width, height = image.size  # type: ignore[attr-defined]
    if max(width, height) <= LONG_EDGE:
        return image
    scale = LONG_EDGE / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.LANCZOS)  # type: ignore[attr-defined]


def process_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Normalize an image to the agent bound. Returns ``(bytes, mime)``.

    HEIC/HEIF becomes JPEG. An image over the long-edge or byte bound is
    downscaled and re-encoded as JPEG. An image already inside the bound keeps
    its bytes and format.
    """
    from PIL import Image

    if mime == "image/heic":
        with Image.open(BytesIO(data)) as heic:
            data = _encode_jpeg(heic, JPEG_QUALITY)
        mime = "image/jpeg"

    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if max(width, height) <= LONG_EDGE and len(data) <= BYTE_BUDGET:
            return data, mime
        fitted = _fit_long_edge(image)
        return _encode_jpeg_to_budget(fitted), "image/jpeg"


# ── the pipeline ─────────────────────────────────────────────────────────────


class AttachmentPipeline:
    """Normalize and store inbound files under the data volume."""

    def __init__(
        self,
        *,
        data_dir: str | Path = "/data",
        retention_days: int = 365,
        keep_originals: bool = False,
        timezone: str = "UTC",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(data_dir)
        self._retention_days = retention_days
        self._keep_originals = keep_originals
        self._tz = ZoneInfo(timezone)
        self._now = clock or (lambda: datetime.now(UTC))

    def _now_local(self) -> datetime:
        """The current time in the configured timezone (the person's wall clock)."""
        return self._now().astimezone(self._tz)

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        """Ingest every file in ``inbox_dir`` off the event loop, then delete it."""
        return await asyncio.to_thread(self._process_dir, inbox_dir, person_id, group_id)

    def _process_dir(
        self, inbox_dir: Path, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        stored: list[Attachment] = []
        if inbox_dir.exists():
            for src in sorted(inbox_dir.iterdir()):
                if not src.is_file():
                    continue
                attachment = self._ingest_one(src, person_id, group_id)
                if attachment is not None:
                    stored.append(attachment)
        _remove_path(inbox_dir)
        return stored

    def _ingest_one(
        self, src: Path, person_id: str | None, group_id: str | None
    ) -> Attachment | None:
        try:
            data = src.read_bytes()
        except OSError as exc:
            logger.warning(
                {"message": "attachment unreadable", "name": src.name, "error": str(exc)}
            )
            return None

        mime = sniff_mime(data)
        decision = classify(mime, len(data))
        if decision != "accept":
            logger.info(
                {
                    "message": "attachment skipped",
                    "reason": decision,
                    "mime": mime,
                    "name": src.name,
                }
            )
            return None
        assert mime is not None

        original = data
        if mime.startswith("image/"):
            try:
                data, mime = process_image(data, mime)
            except Exception as exc:  # noqa: BLE001 - a broken image is skipped, not fatal
                logger.warning(
                    {
                        "message": "image processing failed; skipped",
                        "name": src.name,
                        "error": str(exc),
                    }
                )
                return None

        ext = _MIME_EXT.get(mime, Path(src.name).suffix.lower() or ".bin")
        name = safe_filename(src.name, ext, now=self._now_local())
        _, dest = self._destination(person_id, group_id, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        name = _dedupe_name(dest.parent, name, ext)
        rel, dest = self._destination(person_id, group_id, name)
        dest.write_bytes(data)
        self._write_sidecar(dest, original_name=src.name, mime=mime)
        if self._keep_originals and data is not original:
            self._store_original(dest, src.name, original)
        logger.info({"message": "attachment stored", "path": rel, "mime": mime})
        return Attachment(path=rel, mime=mime, name=name, original_name=src.name)

    def _write_sidecar(self, dest: Path, *, original_name: str, mime: str) -> None:
        """Write ``<dest>.meta.json`` next to a stored attachment.

        The sidecar holds the sender's filename, the sniffed MIME of the stored
        bytes, and the arrival time in UTC. Only ``channels`` sees the
        platform-supplied name, so only ``channels`` can record it; the files
        MCP reads the sidecar to report ``original_name``.

        A failed write does not fail the attachment. The stored bytes are the
        product. The sidecar is metadata, so a failure logs a warning and the
        ingest still returns the ``Attachment``. Both the success and the
        failure log one line at a visible level, so production logs always show
        whether the sidecar landed next to the stored bytes.
        """
        sidecar = dest.parent / f"{dest.name}{META_SUFFIX}"
        meta = {
            "original_name": original_name,
            "mime": mime,
            "received_at": self._now().astimezone(UTC).isoformat(),
        }
        try:
            sidecar.write_text(json.dumps(meta), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - the sidecar is metadata, never fatal
            logger.warning(
                {
                    "message": "attachment sidecar write failed",
                    "path": str(sidecar),
                    "error": str(exc),
                }
            )
            return
        logger.info({"message": "attachment sidecar written", "path": str(sidecar)})

    def _destination(
        self, person_id: str | None, group_id: str | None, name: str
    ) -> tuple[str, Path]:
        """Return the ``(files-MCP relative path, absolute path)`` for a file.

        A DM stores under its person and returns a person-relative path. A group
        stores under the shared root and returns a ``shared/`` path. A file with
        no person and no group (a destination-less event) uses a shared bucket.
        """
        year_month = self._now_local().strftime("%Y/%m")
        if person_id:
            rel = f"attachments/{year_month}/{name}"
            dest = self._root / "people" / person_id / "attachments" / year_month / name
        elif group_id:
            rel = f"shared/attachments/{group_id}/{year_month}/{name}"
            dest = self._root / "shared" / "attachments" / group_id / year_month / name
        else:
            rel = f"shared/attachments/events/{year_month}/{name}"
            dest = self._root / "shared" / "attachments" / "events" / year_month / name
        return rel, dest

    def _store_original(self, dest: Path, raw_name: str, data: bytes) -> None:
        """Store the pre-transform bytes next to the stored file, for archival."""
        ext = Path(posixpath.basename(raw_name.replace("\\", "/"))).suffix.lower() or ".bin"
        originals = dest.parent / "originals"
        name = _dedupe_name(originals, safe_filename(raw_name, ext, now=self._now_local()), ext)
        target = originals / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    # ── maintenance ──────────────────────────────────────────────────────

    def purge_stale_inbox(self, *, older_than_s: float = INBOX_MAX_AGE_S) -> int:
        """Delete inbox scratch older than ``older_than_s``. Returns the count.

        An adapter that died mid-turn can leave an inbox directory behind. This
        clears the leftovers at startup.
        """
        inbox = self._root / "inbox"
        if not inbox.exists():
            return 0
        now = time.time()
        removed = 0
        for child in inbox.iterdir():
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age > older_than_s:
                _remove_path(child)
                removed += 1
        return removed

    def sweep_retention(self) -> int:
        """Delete stored attachments older than the retention window.

        A retention of ``0`` keeps files forever. A sidecar shares its file's
        modification time, so the sweep removes the pair, but the count reports
        attachments only. Returns the count deleted.
        """
        if self._retention_days <= 0:
            return 0
        cutoff = self._now().timestamp() - self._retention_days * 86400
        removed = 0
        for root in self._attachment_roots():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        if not path.name.endswith(META_SUFFIX):
                            removed += 1
                except OSError:
                    continue
        return removed

    def _attachment_roots(self) -> list[Path]:
        roots: list[Path] = []
        shared = self._root / "shared" / "attachments"
        if shared.exists():
            roots.append(shared)
        people = self._root / "people"
        if people.exists():
            for person in people.iterdir():
                attachments = person / "attachments"
                if attachments.exists():
                    roots.append(attachments)
        return roots


def _remove_path(path: Path) -> None:
    """Delete a file or a directory tree, ignoring a missing path."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 - cleanup must never raise into a turn
        logger.warning({"message": "path cleanup failed", "path": str(path), "error": str(exc)})


async def retention_loop(
    pipeline: AttachmentPipeline, *, interval_s: float = RETENTION_INTERVAL_S
) -> None:
    """Run the retention sweep once per ``interval_s``. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            removed = await asyncio.to_thread(pipeline.sweep_retention)
            if removed:
                logger.info({"message": "attachment retention swept", "removed": removed})
        except Exception as exc:  # noqa: BLE001 - a sweep error must not kill the task
            logger.warning({"message": "attachment retention failed", "error": str(exc)})

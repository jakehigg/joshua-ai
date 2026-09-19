"""File an attachment: give it a name that says what it is, and put it where it belongs.

`channels` stores a file under a timestamp and the name the sender's device
chose (`2026-09-16-140509-IMG_4471.jpg`). Once the describer has looked at it,
there is a better name, and for a member there may be a better place.

Two things happen here, both in code and never by the agent:

- **The name.** The slug of the description and the short digest of the bytes:
  `grocery-receipt-d7e122.jpg`. The folder is `YYYY/MM` and the metadata file
  holds the arrival time, so the name carries no date. The digest is what makes
  the name unique, because `core` and `gateway` both write into the wiki folder
  and no check for a free name holds across two containers.
- **The place.** With `attachments.auto_save` on, a member's file moves to
  `wiki/attachments/YYYY/MM/`. It belongs to the wiki then: retention never
  deletes it, and a page can point at it for as long as the page lives. With
  auto-save off, the file is renamed where it is.

A guest's file is never moved. A guest does not write the wiki.

`core` owns the data volume, so `core` does this. The agent has no file tool,
and `channels` has no description to name the file by.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from joshua_shared import layout
from joshua_shared.attachments import (
    AttachmentMeta,
    digest_tag,
    meta_path,
    read_meta,
    write_meta,
)
from joshua_shared.log import get_logger

logger = get_logger("attachments")


def stored_name(current: str, slug: str, sha256: str | None = None) -> str:
    """Return the name for a described file: the slug, the digest, the extension.

    `grocery-receipt-d7e122.jpg`. The folder above it is `YYYY/MM`, and the
    metadata file holds the arrival time to the second, so a date in the name
    says nothing the path does not.

    The short digest of the bytes is what keeps two files apart. `core` and
    `gateway` both write into `wiki/attachments/`, and no check for a free name
    holds across two containers, so the name itself has to be unique. It also
    makes the name idempotent: the same bytes give the same name, so a file
    that is filed twice is one file. The counter in `_free_path` is the
    backstop for the rest.
    """
    tag = digest_tag(sha256)
    stem = f"{slug}-{tag}" if tag else slug
    return f"{stem}{Path(current).suffix}"


def _free_path(directory: Path, name: str, sha256: str | None = None) -> Path:
    """Return the path to write `name` at, without ever losing another file.

    A free name is used as it is. A name taken by a file of the same bytes is
    used too: the digest is in the name, so the file that is there is the file
    that is arriving, and writing it again loses nothing. Any other clash gets
    `-2`, `-3`, and so on.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    if sha256 is not None:
        current = read_meta(candidate)
        if current is not None and current.sha256 == sha256:
            return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while (directory / f"{stem}-{counter}{suffix}").exists():
        counter += 1
    return directory / f"{stem}-{counter}{suffix}"


def _day_of(meta: AttachmentMeta) -> date:
    """The day the file arrived, or today when the metadata does not say."""
    if meta.received_at:
        try:
            return datetime.fromisoformat(meta.received_at).date()
        except ValueError:
            pass
    return datetime.now().date()


def file_attachment(
    rel: str,
    meta: AttachmentMeta,
    *,
    data_root: Path,
    auto_save: bool,
    is_member: bool,
) -> tuple[str, AttachmentMeta]:
    """Name and place one attachment. Returns its `(path, metadata)` after the move.

    `rel` is the path of the file relative to the data volume. The return is
    the path the agent must use from now on, which is `rel` again when nothing
    moved. This never raises: a file that cannot be moved keeps the path it
    has, because the turn has to go on.
    """
    if meta.description is None:
        return rel, meta

    area = layout.attachment_area(rel)
    if area is None:
        return rel, meta

    source = data_root / rel
    if not source.is_file():
        return rel, meta

    name = stored_name(source.name, meta.description.slug, meta.sha256)
    to_wiki = auto_save and is_member and area != "wiki"
    if to_wiki:
        target_dir = layout.month_dir(layout.wiki_attachments_root(data_root), _day_of(meta))
    else:
        target_dir = source.parent
        if name == source.name:
            return rel, meta

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _free_path(target_dir, name, meta.sha256)
        if to_wiki:
            meta.saved_from = rel
        source.replace(target)
        meta_path(source).unlink(missing_ok=True)
        write_meta(target, meta)
    except OSError as exc:
        logger.warning({"message": "attachment not filed", "error": str(exc)})
        return rel, meta

    new_rel = layout.data_relative(target, data_root)
    logger.info({"message": "attachment filed", "from": rel, "to": new_rel, "wiki": to_wiki})
    return new_rel, meta

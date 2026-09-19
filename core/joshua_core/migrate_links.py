"""Move the pictures that wiki pages already point at into the wiki.

Before `wiki/attachments/` existed, a page that showed a photo pointed into the
evidence of a chat: `people/alex/attachments/2026/08/plant.jpg`. Retention
deletes that folder after `channels.limits.attachment_retention_days`, so the
page kept the link and lost the picture.

This runs at each start. For every link in a wiki page that points into an
attachments folder outside the wiki, it copies the file into
`wiki/attachments/YYYY/MM/` and rewrites the link. The file the person sent is
evidence and is never moved.

It is safe to run again: a page with no such link is not read twice, a file
already copied is not copied again, and a page is written only when a link
changed.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from joshua_shared import layout
from joshua_shared.attachments import (
    AttachmentMeta,
    digest_tag,
    is_meta,
    read_meta,
    sha256_file,
    write_meta,
)
from joshua_shared.log import get_logger

logger = get_logger("migrate_links")

# A markdown link or image whose target points into an attachments folder:
# `![a](people/alex/attachments/2026/08/x.jpg)` or `(/shared/attachments/…)`.
_LINK_RE = re.compile(
    r"\((?P<prefix>/?)(?P<path>(?:people/[a-z0-9][a-z0-9-]{0,31}|shared)/attachments/[^)\s]+)\)"
)


def _target_name(source: Path, sha256: str | None) -> str:
    """The name of the copy: the name it has, plus the short digest of its bytes."""
    tag = digest_tag(sha256)
    if not tag or source.stem.endswith(f"-{tag}"):
        return source.name
    return f"{source.stem}-{tag}{source.suffix}"


def _free_path(directory: Path, name: str, sha256: str | None = None) -> Path:
    """The path to write at. A name held by the same bytes is used again."""
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


def _month_of(rel: str) -> tuple[str, str] | None:
    """The `YYYY`, `MM` a stored attachment path carries, if it has one."""
    parts = rel.split("/")
    for index in range(len(parts) - 2):
        year, month = parts[index], parts[index + 1]
        if len(year) == 4 and year.isdigit() and len(month) == 2 and month.isdigit():
            return year, month
    return None


def _copy_into_wiki(rel: str, data_root: Path, done: dict[str, str]) -> str | None:
    """Copy one attachment into the wiki and return its new link, or None."""
    if rel in done:
        return done[rel]
    if layout.attachment_area(rel) != "person" and layout.attachment_area(rel) != "shared":
        return None
    source = data_root / rel
    if not source.is_file() or is_meta(source):
        return None

    month = _month_of(rel)
    target_dir = layout.wiki_attachments_root(data_root)
    target_dir = target_dir / month[0] / month[1] if month else target_dir
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        meta = read_meta(source) or AttachmentMeta(mime="application/octet-stream")
        if not meta.sha256:
            meta.sha256 = sha256_file(source)
        target = _free_path(target_dir, _target_name(source, meta.sha256), meta.sha256)
        shutil.copy2(source, target)
        meta.saved_from = rel
        write_meta(target, meta)
    except OSError as exc:
        logger.warning({"message": "attachment not copied into the wiki", "error": str(exc)})
        return None

    link = "/" + layout.data_relative(target, data_root)[len("wiki/") :]
    done[rel] = link
    return link


def migrate_page_links(root: Path | str | None = None) -> dict[str, int]:
    """Copy every attachment a wiki page links to into the wiki. Idempotent.

    Returns counts for `pages` and `files`, and logs one line when anything
    moved.
    """
    data_root = layout.data_root() if root is None else Path(root)
    wiki = layout.wiki_root(data_root)
    counts = {"pages": 0, "files": 0}
    if not wiki.is_dir():
        return counts

    done: dict[str, str] = {}
    for page in sorted(wiki.rglob("*.md")):
        if not page.is_file() or layout.is_hidden(page, wiki):
            continue
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "attachments/" not in text:
            continue

        changed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            link = _copy_into_wiki(match.group("path"), data_root, done)
            if link is None:
                return match.group(0)
            changed = True
            return f"({link})"

        new_text = _LINK_RE.sub(_replace, text)
        if changed and new_text != text:
            try:
                page.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                logger.warning({"message": "page link not rewritten", "error": str(exc)})
                continue
            counts["pages"] += 1

    counts["files"] = len(done)
    if counts["pages"]:
        logger.info({"message": "wiki page links moved into the wiki", **counts})
    return counts

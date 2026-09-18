"""Read the text layer of one PDF, in a process of its own.

``extract.py`` starts this module as a subprocess. A PDF is untrusted input:
it can hold a loop or a structure that makes a parser use all the memory and
the time of the machine. A subprocess gives the work a memory limit, a CPU
limit, and a wall-clock timeout that the parent applies, and a crash here
never touches ``channels``.

Usage, and nobody calls it by hand::

    python -m joshua_channels.pdf_text <path> <max_pages> <max_chars>

It prints one JSON object on stdout: ``{"text": str, "pages": int,
"truncated": bool}``. Any failure exits non-zero with no output.
"""

from __future__ import annotations

import json
import resource
import sys
from pathlib import Path

# The address space and the CPU time of this process. A PDF that needs more
# than this is not read.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 20


def _limit() -> None:
    """Cap the memory and the CPU time of this process."""
    for name, limit in (
        ("RLIMIT_AS", MEMORY_LIMIT_BYTES),
        ("RLIMIT_CPU", CPU_LIMIT_SECONDS),
    ):
        key = getattr(resource, name, None)
        if key is None:
            continue
        try:
            soft, hard = resource.getrlimit(key)
            ceiling = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(key, (ceiling, hard))
        except (ValueError, OSError):
            # A platform that refuses the limit still gets the timeout of the
            # parent, which is the limit that matters most.
            continue


def extract(path: Path, max_pages: int, max_chars: int) -> dict[str, object]:
    """Return the text of the first ``max_pages`` pages, cut to ``max_chars``."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total = len(reader.pages)
    parts: list[str] = []
    size = 0
    truncated = total > max_pages
    for page in reader.pages[:max_pages]:
        text = page.extract_text() or ""
        if size + len(text) > max_chars:
            parts.append(text[: max_chars - size])
            truncated = True
            break
        parts.append(text)
        size += len(text)
    return {"text": "\n".join(parts), "pages": total, "truncated": truncated}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    _limit()
    try:
        result = extract(Path(argv[1]), int(argv[2]), int(argv[3]))
    except Exception:  # noqa: BLE001 - any parser failure is "no text"
        return 1
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point of the subprocess
    raise SystemExit(main(sys.argv))

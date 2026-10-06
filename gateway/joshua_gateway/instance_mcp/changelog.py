"""Read ``docs/CHANGELOG.md`` and keep the sections newer than a version.

A section starts with ``## <version> - <date>``. ``## Unreleased`` is not a
release and is left out. Inside a section, an ``### Upgrading from`` part is
what a person must do, so it moves to the front of its section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from joshua_gateway.instance_mcp import versions

_SECTION_RE = re.compile(r"^## (\S+)(.*)$")
_UPGRADING_RE = re.compile(r"^### Upgrading from", re.IGNORECASE)

# How much changelog text one answer may carry.
MAX_CHARS = 12_000


@dataclass(frozen=True)
class Section:
    version: str
    heading: str
    body: str

    def text(self) -> str:
        return f"{self.heading}\n{self.body}".rstrip() + "\n"


def split(markdown: str) -> list[Section]:
    """The release sections of a changelog, newest first as the file lists them."""
    sections: list[Section] = []
    current: tuple[str, str, list[str]] | None = None
    for line in markdown.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            if current is not None:
                sections.append(_finish(*current))
            current = (match.group(1), line, [])
            continue
        if current is not None:
            current[2].append(line)
    if current is not None:
        sections.append(_finish(*current))
    return [s for s in sections if versions.parse(s.version) is not None]


def _finish(version: str, heading: str, lines: list[str]) -> Section:
    return Section(version=version, heading=heading, body=_upgrading_first("\n".join(lines)))


def _upgrading_first(body: str) -> str:
    """The body with every ``### Upgrading from`` part ahead of the other parts."""
    parts: list[list[str]] = [[]]
    for line in body.splitlines():
        if line.startswith("### "):
            parts.append([])
        parts[-1].append(line)
    lead, rest = parts[0], parts[1:]
    upgrading = [p for p in rest if _UPGRADING_RE.match(p[0])]
    others = [p for p in rest if not _UPGRADING_RE.match(p[0])]
    ordered = [lead, *upgrading, *others]
    return "\n".join(line for part in ordered for line in part).strip("\n")


def newer_than(markdown: str, running: str | None, limit: int = MAX_CHARS) -> str:
    """The sections newer than ``running``, newest first, within ``limit`` chars.

    With no parseable ``running`` version, the newest section alone, because a
    branch build or a dev build has no place in the list.
    """
    sections = split(markdown)
    if versions.parse(running) is None:
        chosen = sections[:1]
    else:
        chosen = [s for s in sections if versions.is_newer(s.version, running)]
    out: list[str] = []
    used = 0
    for section in chosen:
        text = section.text()
        if used + len(text) > limit:
            out.append(f"## {section.version}\n(section left out: the answer is at its limit)\n")
            break
        out.append(text)
        used += len(text)
    return "\n".join(out).rstrip()

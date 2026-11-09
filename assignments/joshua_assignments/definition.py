"""The definition of an assignment as YAML text, with stable line numbers.

A person comments on a line of a definition in the viewer, and Joshua reads
the comment with ``assignment_comments``. Both read the same text from
:func:`render`, so a line number means the same line everywhere. The read API
gives the text as ``definition``, and the viewer shows it as it is.

The order of the keys is fixed: :data:`KEY_ORDER` first, then any other key in
alphabetical order. The keys of a nested mapping are in alphabetical order.
``goal`` and every string with more than one line are block scalars (``|-``),
one line of the text to one line of YAML. A string that YAML could read as
another type, or that holds a character YAML gives a meaning to, is written
in double quotes. The text has no line at the end with no content.

Each function is pure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

# The keys of a spec in the order that the definition shows them.
KEY_ORDER = (
    "title",
    "goal",
    "model",
    "budget",
    "max_turns",
    "working_time",
    "repos",
    "tools",
    "tasks_project",
    "report_to",
    "dry_run",
)
# The keys that are always a block scalar.
BLOCK_KEYS = frozenset({"goal"})
INDENT = "  "
# A string that can stay with no quotes: it starts with a letter, a digit, or
# one of ``_./~`` that YAML gives no meaning at the start, and it holds no
# ``: `` and no `` #``.
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9 _./@:+%,()'-]*\Z")
# Words that YAML 1.1 or 1.2 reads as a bool or a null.
_RESERVED = frozenset(
    {"true", "false", "yes", "no", "on", "off", "y", "n", "null", "none", "~", ".nan", ".inf"}
)
_NUMERIC_LIKE_RE = re.compile(r"^[\d:._+-]+\Z")
_NUMBER_RE = re.compile(r"^[-+]?(\d[\d_]*)?(\.\d*)?([eE][-+]?\d+)?\Z|^0[xob][0-9a-fA-F_]+\Z")


def _ordered_keys(mapping: Mapping[str, Any], *, top: bool) -> list[str]:
    keys = [str(key) for key in mapping]
    if not top:
        return sorted(keys)
    known = [key for key in KEY_ORDER if key in mapping]
    return known + sorted(key for key in keys if key not in KEY_ORDER)


def _plain_ok(text: str) -> bool:
    if not text or text != text.strip() or not _PLAIN_RE.match(text):
        return False
    if ": " in text or text.endswith(":") or " #" in text:
        return False
    if text.lower() in _RESERVED or _NUMBER_RE.match(text):
        return False
    # A date, a time, or a number with colons in YAML 1.1.
    return not _NUMERIC_LIKE_RE.match(text)


def scalar(value: Any) -> str:
    """One value that is not a mapping or a list, as YAML on one line."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    text = str(value)
    return text if _plain_ok(text) else json.dumps(text, ensure_ascii=False)


def _lines_of(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _block(text: str, indent: str) -> tuple[str, list[str]]:
    """The header (``|-``, or ``|2-`` for a first line that starts with a
    space) and the lines of a block scalar."""
    lines = _lines_of(text.rstrip("\n"))
    first = next((line for line in lines if line.strip()), "")
    header = f"|{len(INDENT)}-" if first[:1] in (" ", "\t") else "|-"
    return header, [f"{indent}{INDENT}{line}" if line else "" for line in lines]


def _emit(key: str, value: Any, indent: str, out: list[str], *, block: bool = False) -> None:
    prefix = f"{indent}{scalar(key)}:"
    if isinstance(value, Mapping):
        if not value:
            out.append(f"{prefix} {{}}")
            return
        out.append(prefix)
        for name in _ordered_keys(value, top=False):
            _emit(name, value[name], indent + INDENT, out)
        return
    if isinstance(value, list | tuple):
        if not value:
            out.append(f"{prefix} []")
            return
        out.append(prefix)
        for entry in value:
            _emit_item(entry, indent + INDENT, out)
        return
    if isinstance(value, str) and value.strip() and (block or "\n" in value or "\r" in value):
        header, lines = _block(value, indent)
        out.append(f"{prefix} {header}")
        out.extend(lines)
        return
    out.append(f"{prefix} {scalar(value)}")


def _emit_item(value: Any, indent: str, out: list[str]) -> None:
    """One entry of a list. A mapping or a list in a list is JSON on one
    line, which is also YAML: a spec has no such value."""
    if isinstance(value, Mapping | list | tuple):
        out.append(f"{indent}- {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    elif isinstance(value, str) and ("\n" in value or "\r" in value):
        header, lines = _block(value, indent)
        out.append(f"{indent}- {header}")
        out.extend(lines)
    else:
        out.append(f"{indent}- {scalar(value)}")


def render(spec: Mapping[str, Any] | None) -> str:
    """The definition: ``spec`` as YAML text, the same for the same spec."""
    out: list[str] = []
    spec = spec or {}
    for key in _ordered_keys(spec, top=True):
        _emit(key, spec[key], "", out, block=key in BLOCK_KEYS and isinstance(spec[key], str))
    return "\n".join(out)


def lines(spec: Mapping[str, Any] | None) -> list[str]:
    """The lines of :func:`render`. Line 1 is ``lines(spec)[0]``."""
    text = render(spec)
    return text.split("\n") if text else []


def line_at(definition: list[str], line_no: int) -> str | None:
    """The line ``line_no`` (from 1) of ``definition``, or None when there is none."""
    if 1 <= line_no <= len(definition):
        return definition[line_no - 1]
    return None

"""Clean the text fields of a worker answer of markup that leaked into them.

A model that answers with a schema sometimes writes the next fields of its
answer as tags inside the text of one field: `...</answer>\\n<answer_short>...`,
or a tool call fragment such as `<parameter name="sources">`. The schema still
holds, so the answer validates, and the leaked text goes on to a page.

`clean_field` cuts a text at the first leaked tag and removes the tags that are
left. `recover_fields` reads the leaked copy of a field, so that a caller can
fill a field that the model left empty. `CleanModel` applies `clean_field` to
each text field of an answer model, and `run_worker` logs one line for each
answer that needed it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, ClassVar

from joshua_shared.log import get_logger
from pydantic import BaseModel, ValidationInfo, field_validator

logger = get_logger("engine.sanitize")

# The HTML elements that a page can use. A tag of one of these is never a
# leaked field.
KEEP_TAGS = frozenset(
    {
        "b",
        "br",
        "code",
        "del",
        "details",
        "em",
        "i",
        "kbd",
        "li",
        "mark",
        "ol",
        "p",
        "s",
        "small",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)

# The key of the validation context that collects the names of cleaned fields.
CLEANED = "cleaned"

_CLOSE_RE = re.compile(r"</([a-z_]+)>")
_FRAGMENT_RE = re.compile(r"<parameter name=|<parameter_name>|<invoke ")
_TAG_RE = re.compile(r"</?([a-z_]{2,40})>")


def _first_cut(text: str) -> int:
    """The index of the first leaked closing tag or tool call fragment, or the
    length of `text` when there is none."""
    cut = len(text)
    for match in _CLOSE_RE.finditer(text):
        if match.group(1) not in KEEP_TAGS:
            cut = match.start()
            break
    fragment = _FRAGMENT_RE.search(text)
    if fragment is not None:
        cut = min(cut, fragment.start())
    return cut


def clean_field(text: str) -> str:
    """`text` cut at the first leaked closing tag (`</answer>`) or tool call
    fragment (`<parameter name=`, `<parameter_name>`, `<invoke `), with each
    other tag of a field name removed, and trimmed.

    A tag in `KEEP_TAGS` stays. A `<` before a space or a digit is not a tag."""
    kept = text[: _first_cut(text)]
    kept = _TAG_RE.sub(lambda m: m.group(0) if m.group(1) in KEEP_TAGS else "", kept)
    return kept.strip()


def _opening(name: str) -> re.Pattern[str]:
    escaped = re.escape(name)
    return re.compile(rf"<{escaped}>|<parameter name=\"{escaped}\">")


def _next_tag(text: str) -> int:
    """The index of the first tag in `text` that is not in `KEEP_TAGS`, or the
    length of `text`."""
    for match in _TAG_RE.finditer(text):
        if match.group(1) not in KEEP_TAGS:
            return match.start()
    return len(text)


def recover_fields(text: str, names: Sequence[str]) -> dict[str, str]:
    """The leaked copy of each field of `names` in `text`, cleaned.

    A block starts at `<name>` or `<parameter name="name">`. It ends at its
    closing tag, or, when there is none, at the next tag or at the end of the
    text. A name with no block, or with an empty block, is not in the result."""
    found: dict[str, str] = {}
    for name in names:
        start = _opening(name).search(text)
        if start is None:
            continue
        rest = text[start.end() :]
        end = rest.find(f"</{name}>")
        if end < 0:
            end = rest.find("</parameter>")
        if end < 0:
            end = _next_tag(rest)
        value = clean_field(rest[:end])
        if value:
            found[name] = value
    return found


def recovered_list(text: str) -> list[str]:
    """A recovered list field as its items: a JSON list of strings, or one
    item for each line, without a leading `-` or `*`."""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [clean_field(str(item)) for item in parsed if str(item).strip()]
    lines = (line.strip().lstrip("-*").strip() for line in text.splitlines())
    return [line for line in lines if line]


def note(info: ValidationInfo | None, name: str) -> None:
    """Record that the field `name` was cleaned, when the validation has a
    `CLEANED` context."""
    context = info.context if info is not None else None
    if isinstance(context, dict) and isinstance(context.get(CLEANED), set):
        context[CLEANED].add(name)


def cleaning_context() -> dict[str, set[str]]:
    """A validation context that collects the names of the cleaned fields."""
    return {CLEANED: set()}


def log_cleaned(worker: str, context: dict[str, Any]) -> None:
    """One log line for an answer that needed cleaning. It holds the field
    names only."""
    fields = sorted(context.get(CLEANED) or ())
    if fields:
        logger.info({"message": "worker answer cleaned", "worker": worker, "fields": fields})


class CleanModel(BaseModel):
    """An answer model whose text fields are cleaned with `clean_field`.

    Each `str` field and each `str` item of a `list[str]` field is cleaned,
    except the fields in `raw_fields`, which stay byte for byte. A field in
    `url_fields` is only trimmed."""

    raw_fields: ClassVar[frozenset[str]] = frozenset()
    url_fields: ClassVar[frozenset[str]] = frozenset()

    @field_validator("*", mode="after")
    @classmethod
    def _clean(cls, value: Any, info: ValidationInfo) -> Any:
        name = info.field_name or ""
        if name in cls.raw_fields:
            return value
        if name in cls.url_fields:
            return value.strip() if isinstance(value, str) else value
        if isinstance(value, str):
            return _clean_one(value, name, info)
        if isinstance(value, list) and any(isinstance(item, str) for item in value):
            return [
                _clean_one(item, name, info) if isinstance(item, str) else item for item in value
            ]
        return value


def _clean_one(value: str, name: str, info: ValidationInfo) -> str:
    cleaned = clean_field(value)
    if cleaned != value.strip():
        note(info, name)
    return cleaned

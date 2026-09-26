"""Write the ``/data/people.yaml`` roster people.

The people file holds runtime people additions in the same schema as
``people``. The config loader merges it over ``joshua.yaml`` at load
time (``joshua_shared.config.read_people_file``); this module writes it.
``upsert_person`` adds or replaces a person; ``mark_removed`` writes a
``role: removed`` tombstone that drops the person from the merged roster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import strictyaml
from joshua_shared.config import read_people_file


def write_people_file(path: Path, people: list[dict[str, Any]]) -> None:
    """Write the roster list to the people file, creating the parent dir.

    An empty roster writes an empty file, which reads back as [].
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = strictyaml.as_document({"people": people}).as_yaml() if people else ""
    path.write_text(text)


def merge_handles(current: dict[str, Any], added: dict[str, Any]) -> dict[str, Any]:
    """Union ``added`` into ``current``, one channel type at a time.

    A value is one handle or a list of handles. A second handle on a type the
    person already has is appended, never swapped in, so the roster keeps the
    handle Joshua already answers.
    """
    merged: dict[str, Any] = {}
    for handle_type in [*current, *(t for t in added if t not in current)]:
        ids: list[str] = []
        for source in (current.get(handle_type), added.get(handle_type)):
            values = [source] if isinstance(source, str) else list(source or [])
            ids.extend(v for v in values if v not in ids)
        merged[handle_type] = ids[0] if len(ids) == 1 else ids
    return merged


def upsert_person(path: Path, entry: dict[str, Any]) -> None:
    """Add ``entry`` to the people, or update the entry with the same id.

    An update merges handles so a second handle for a person does not drop the
    first, and clears a prior ``role: removed`` tombstone.
    """
    people = read_people_file(path)
    out: list[dict[str, Any]] = []
    merged = False
    for person in people:
        if person.get("id") != entry["id"]:
            out.append(person)
            continue
        updated = {**person, **{k: v for k, v in entry.items() if k != "handles"}}
        handles = merge_handles(person.get("handles") or {}, entry.get("handles") or {})
        if handles:
            updated["handles"] = handles
        out.append(updated)
        merged = True
    if not merged:
        out.append(entry)
    write_people_file(path, out)


def mark_removed(path: Path, person_id: str) -> None:
    """Drop ``person_id`` from the people file and write a ``role: removed`` tombstone.

    The tombstone overrides a person defined in ``joshua.yaml``, so the merged
    roster no longer carries them.
    """
    people = read_people_file(path)
    out = [p for p in people if p.get("id") != person_id]
    out.append({"id": person_id, "role": "removed"})
    write_people_file(path, out)

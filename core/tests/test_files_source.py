"""Offline tests for the ``files`` source adapter over a temp data volume."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from joshua_core.memory.sources.files import FilesSource


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path) -> None:
    _write(root / "wiki/pizza.md", "# Pizza\n\nDough and sauce recipe notes here.\n")
    _write(root / "wiki/joshua-docs/quickstart.md", "# Quickstart\n\nThe shipped documentation.\n")
    _write(
        root / "wiki/journal/2026/08/24/2026-08-24.md",
        "---\ndate: 2026-08-24\npeople: [alice]\nsource: nightly\n---\n"
        "# 2026-08-24\n\nReflections from the day, long enough.\n",
    )
    _write(
        root / "wiki/journal/2026/08/23/garden.md",
        "---\npeople: [bob]\nsource: agent\n---\n# Garden\n\nBob's journal entry, long enough.\n",
    )
    _write(root / "wiki/people/alice.md", "# Alice\n\nJoshua's profile of Alice.\n")
    _write(root / "wiki/people/everyone.md", "# Home\n\nThe shared profile.\n")
    _write(root / "shared/house.md", "# House\n\nA compatibility-only shared page.\n")
    # Ignored: trash, a non-markdown metadata, and inbound attachments (not a
    # retrieval kind).
    _write(root / "wiki/.trash/old.md", "# Old\n\nDeleted content still on disk.\n")
    _write(root / "wiki/pizza.meta.json", '{"x": 1}')
    _write(root / "people/alice/attachments/note.md", "# Attach\n\nNot a retrieval kind.\n")


async def _docs(root: Path) -> list:
    src = FilesSource(root)
    return [d async for d in src.list_documents()]


async def test_lists_the_wiki_all_shared_scope(tmp_path: Path) -> None:
    _tree(tmp_path)
    docs = await _docs(tmp_path)
    by_uri = {d.uri: d for d in docs}

    assert "wiki/pizza.md" in by_uri
    assert "wiki/joshua-docs/quickstart.md" in by_uri
    assert "wiki/journal/2026/08/24/2026-08-24.md" in by_uri
    assert "wiki/journal/2026/08/23/garden.md" in by_uri
    assert "wiki/people/alice.md" in by_uri
    assert "wiki/people/everyone.md" in by_uri
    assert "shared/house.md" in by_uri
    # Every document is shared scope: there is no per-person content any more.
    assert all(d.person_id is None for d in docs)
    # Ignored paths never appear.
    assert not any(".trash" in d.uri for d in docs)
    assert not any(d.uri.startswith("attachments/") for d in docs)
    assert not any("people/alice/attachments" in d.uri for d in docs)


async def test_document_fields(tmp_path: Path) -> None:
    _tree(tmp_path)
    docs = {d.uri: d for d in await _docs(tmp_path)}

    pizza = docs["wiki/pizza.md"]
    assert pizza.source == "files"
    assert pizza.title == "Pizza"  # first '# ' heading
    assert pizza.provenance == "own"
    assert pizza.rev and len(pizza.rev) == 64  # sha256 hex

    entry = docs["wiki/journal/2026/08/24/2026-08-24.md"]
    assert entry.doc_date == date(2026, 8, 24)  # from the journal day, not the frontmatter


async def test_journal_doc_date_comes_from_the_day_folder_not_the_name(tmp_path: Path) -> None:
    """An entry's file name is a slug, not a date — only the folder says the
    day, with no frontmatter `date` at all here."""
    _tree(tmp_path)
    docs = {d.uri: d for d in await _docs(tmp_path)}
    entry = docs["wiki/journal/2026/08/23/garden.md"]
    assert entry.doc_date == date(2026, 8, 23)


async def test_journal_doc_date_beats_a_conflicting_frontmatter_date(tmp_path: Path) -> None:
    """The day folder is the source of truth; a stray `date` in the entry's own
    frontmatter never overrides it."""
    _write(
        tmp_path / "wiki/journal/2026/08/23/mismatch.md",
        "---\ndate: 2020-01-01\npeople: [bob]\nsource: agent\n---\n# Mismatch\n\nBody.\n",
    )
    docs = {d.uri: d for d in await _docs(tmp_path)}
    assert docs["wiki/journal/2026/08/23/mismatch.md"].doc_date == date(2026, 8, 23)


async def test_non_journal_doc_date_falls_back_to_frontmatter(tmp_path: Path) -> None:
    _write(
        tmp_path / "wiki/import.md",
        "---\ndate: 2026-01-05\ntags: [recipes, import]\nprovenance: external\n---\n"
        "# Imported\n\nA recipe imported from outside.\n",
    )
    docs = {d.uri: d for d in await _docs(tmp_path)}
    doc = docs["wiki/import.md"]
    assert doc.provenance == "external"
    assert doc.doc_date == date(2026, 1, 5)
    assert doc.tags == ("recipes", "import")
    assert doc.text.startswith("# Imported")  # frontmatter stripped from the body


async def test_a_wiki_page_with_no_date_has_none(tmp_path: Path) -> None:
    _write(tmp_path / "wiki/plain.md", "# Plain\n\nNo frontmatter at all.\n")
    docs = {d.uri: d for d in await _docs(tmp_path)}
    assert docs["wiki/plain.md"].doc_date is None


async def test_fetch_resolves_by_path(tmp_path: Path) -> None:
    _tree(tmp_path)
    src = FilesSource(tmp_path)
    assert (await src.fetch("shared/house.md")).person_id is None
    assert (await src.fetch("wiki/pizza.md")).person_id is None
    entry = await src.fetch("wiki/journal/2026/08/24/2026-08-24.md")
    assert entry is not None
    assert entry.person_id is None
    assert entry.doc_date == date(2026, 8, 24)
    assert await src.fetch("wiki/missing.md") is None
    assert await src.fetch("people/alice/attachments/note.md") is None  # not a retrieval kind


async def test_dot_entries_are_ignored_at_any_depth(tmp_path: Path) -> None:
    """A wiki frontend's own state (``.git``, ``.obsidian``, and so on) is never
    a document, however deep it sits."""
    _write(tmp_path / "wiki/plants/a.md", "# Plants\n\nA normal wiki page.\n")
    _write(tmp_path / "wiki/.git/config", "not markdown\n")
    _write(tmp_path / "wiki/.git/refs/x.md", "# Ref\n\nGit's own state, not content.\n")
    _write(tmp_path / "wiki/.obsidian/workspace.md", "# Workspace\n\nA note tool's state.\n")
    _write(tmp_path / "wiki/journal/.trash/old.md", "# Old\n\nTrashed journal entry.\n")
    docs = await _docs(tmp_path)
    uris = {d.uri for d in docs}
    assert "wiki/plants/a.md" in uris
    assert not any(".git" in d.uri for d in docs)
    assert not any(".obsidian" in d.uri for d in docs)
    assert not any(".trash" in d.uri for d in docs)


async def test_fetch_refuses_a_dot_entry(tmp_path: Path) -> None:
    _write(tmp_path / "wiki/.git/config", "not markdown\n")
    src = FilesSource(tmp_path)
    assert await src.fetch("wiki/.git/config") is None


async def test_rev_changes_with_content(tmp_path: Path) -> None:
    f = tmp_path / "wiki/pizza.md"
    _write(f, "# Pizza\n\nFirst version of the recipe body.\n")
    first = (await _docs(tmp_path))[0].rev
    _write(f, "# Pizza\n\nSecond, edited version of the recipe body.\n")
    second = (await _docs(tmp_path))[0].rev
    assert first != second


async def test_no_wiki_or_shared_tree_yields_nothing(tmp_path: Path) -> None:
    """A brand-new data dir with neither tree yet is a plain empty listing, not
    an error."""
    assert await _docs(tmp_path) == []

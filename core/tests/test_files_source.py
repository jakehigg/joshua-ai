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
    _write(root / "wiki/joshua/quickstart.md", "# Quickstart\n\nThe shipped documentation.\n")
    _write(root / "people/alice/blog/2026-08-24.md", "Reflections from the day, long enough.\n")
    _write(root / "people/bob/blog/2026-08-23.md", "Bob's private journal entry, long enough.\n")
    _write(root / "shared/profile.md", "# Home\n\nShared profile content.\n")
    # Ignored: trash, non-markdown sidecar, non-indexed kinds, and a legacy
    # per-person wiki that bootstrap has not moved yet.
    _write(root / "wiki/.trash/old.md", "# Old\n\nDeleted content still on disk.\n")
    _write(root / "wiki/pizza.meta.json", '{"x": 1}')
    _write(root / "people/alice/attachments/note.md", "# Attach\n\nNot a retrieval kind.\n")
    _write(root / "people/alice/wiki/legacy.md", "# Legacy\n\nA per-person wiki page.\n")
    # A stray non-slug directory under people/ is not a person.
    _write(root / "people/NotASlug!/blog/x.md", "# Nope\n\nInvalid person id directory.\n")


async def _docs(root: Path) -> list:
    src = FilesSource(root)
    return [d async for d in src.list_documents()]


async def test_lists_person_and_shared_scopes(tmp_path: Path) -> None:
    _tree(tmp_path)
    docs = await _docs(tmp_path)
    by_path = {(d.person_id, d.uri): d for d in docs}

    assert (None, "wiki/pizza.md") in by_path  # the wiki is shared scope
    assert (None, "wiki/joshua/quickstart.md") in by_path
    assert ("alice", "blog/2026-08-24.md") in by_path
    assert ("bob", "blog/2026-08-23.md") in by_path
    assert (None, "shared/profile.md") in by_path
    # Ignored paths never appear.
    assert not any(".trash" in d.uri for d in docs)
    assert not any(d.uri.startswith("attachments/") for d in docs)
    assert not any("legacy" in d.uri for d in docs)
    assert not any("NotASlug" in (d.person_id or "") for d in docs)


async def test_document_fields(tmp_path: Path) -> None:
    _tree(tmp_path)
    docs = {(d.person_id, d.uri): d for d in await _docs(tmp_path)}

    pizza = docs[(None, "wiki/pizza.md")]
    assert pizza.source == "files"
    assert pizza.title == "Pizza"  # first '# ' heading
    assert pizza.provenance == "own"
    assert pizza.rev and len(pizza.rev) == 64  # sha256 hex

    blog = docs[("alice", "blog/2026-08-24.md")]
    assert blog.title == "2026-08-24"  # no heading → filename stem
    assert blog.doc_date == date(2026, 8, 24)  # parsed from the blog name


async def test_frontmatter_is_honored(tmp_path: Path) -> None:
    _write(
        tmp_path / "wiki/import.md",
        "---\ndate: 2026-01-05\ntags: [recipes, import]\nprovenance: external\n---\n"
        "# Imported\n\nA recipe imported from outside.\n",
    )
    docs = {(d.person_id, d.uri): d for d in await _docs(tmp_path)}
    doc = docs[(None, "wiki/import.md")]
    assert doc.provenance == "external"
    assert doc.doc_date == date(2026, 1, 5)
    assert doc.tags == ("recipes", "import")
    assert doc.text.startswith("# Imported")  # frontmatter stripped from the body


async def test_fetch_resolves_by_path(tmp_path: Path) -> None:
    _tree(tmp_path)
    src = FilesSource(tmp_path)
    assert (await src.fetch("shared/profile.md")).person_id is None
    assert (await src.fetch("wiki/pizza.md")).person_id is None
    assert (await src.fetch("blog/2026-08-24.md")).person_id == "alice"
    assert await src.fetch("wiki/missing.md") is None
    assert await src.fetch("attachments/note.md") is None  # not a retrieval kind


async def test_dot_entries_are_ignored_at_any_depth(tmp_path: Path) -> None:
    """A wiki frontend's own state (``.git``, ``.obsidian``, and so on) is never
    a document, in the wiki or in a person's journal, however deep it sits."""
    _write(tmp_path / "wiki/plants/a.md", "# Plants\n\nA normal wiki page.\n")
    _write(tmp_path / "wiki/.git/config", "not markdown\n")
    _write(tmp_path / "wiki/.git/refs/x.md", "# Ref\n\nGit's own state, not content.\n")
    _write(tmp_path / "wiki/.obsidian/workspace.md", "# Workspace\n\nA note tool's state.\n")
    _write(tmp_path / "people/alice/blog/.trash/old.md", "# Old\n\nTrashed journal entry.\n")
    docs = await _docs(tmp_path)
    by_uri = {(d.person_id, d.uri) for d in docs}
    assert (None, "wiki/plants/a.md") in by_uri
    assert not any(".git" in d.uri for d in docs)
    assert not any(".obsidian" in d.uri for d in docs)
    assert not any(".trash" in d.uri for d in docs)


async def test_fetch_refuses_a_dot_entry(tmp_path: Path) -> None:
    _write(tmp_path / "wiki/.git/config", "not markdown\n")
    _write(tmp_path / "people/alice/blog/.trash/old.md", "# Old\n\nTrashed entry.\n")
    src = FilesSource(tmp_path)
    assert await src.fetch("wiki/.git/config") is None
    assert await src.fetch("blog/.trash/old.md") is None


async def test_rev_changes_with_content(tmp_path: Path) -> None:
    f = tmp_path / "wiki/pizza.md"
    _write(f, "# Pizza\n\nFirst version of the recipe body.\n")
    first = (await _docs(tmp_path))[0].rev
    _write(f, "# Pizza\n\nSecond, edited version of the recipe body.\n")
    second = (await _docs(tmp_path))[0].rev
    assert first != second


# -- a directory that names no person ------------------------------------------


async def _docs_with_roster(root: Path, roster: set[str]) -> list:
    async def persons() -> set[str]:
        return roster

    src = FilesSource(root, persons=persons)
    return [d async for d in src.list_documents()]


async def test_a_directory_that_names_no_person_is_skipped(tmp_path: Path) -> None:
    """A chunk carries a foreign key to people, so such a document is unstorable."""
    _tree(tmp_path)
    _write(tmp_path / "people/Ghost/blog/x.md", "# Ghost\n\nAn orphan directory.\n")
    docs = await _docs_with_roster(tmp_path, {"alice", "bob"})
    assert {d.person_id for d in docs if d.person_id is not None} == {"alice", "bob"}


async def test_the_real_documents_still_index_beside_an_orphan(tmp_path: Path) -> None:
    _tree(tmp_path)
    _write(tmp_path / "people/ghost/blog/x.md", "# Ghost\n\nAn orphan directory.\n")
    docs = await _docs_with_roster(tmp_path, {"alice", "bob"})
    assert ("alice", "blog/2026-08-24.md") in {(d.person_id, d.uri) for d in docs}
    assert ("bob", "blog/2026-08-23.md") in {(d.person_id, d.uri) for d in docs}


async def test_the_skip_is_one_line_for_the_pass(tmp_path: Path, caplog) -> None:
    """One stray directory made a line for each of its documents, on every pass."""
    _tree(tmp_path)
    for name in ("x", "y", "z"):
        _write(tmp_path / f"people/ghost/blog/{name}.md", f"# {name}\n\nAn orphan post.\n")
    with caplog.at_level("WARNING"):
        await _docs_with_roster(tmp_path, {"alice", "bob"})
    lines = [r for r in caplog.records if "name no person" in r.getMessage()]
    assert len(lines) == 1


async def test_no_roster_walks_every_slug_directory(tmp_path: Path) -> None:
    """Without a roster the adapter is a plain walker, which the unit tests want."""
    _tree(tmp_path)
    _write(tmp_path / "people/ghost/blog/x.md", "# Ghost\n\nAn orphan directory.\n")
    docs = await _docs(tmp_path)
    assert "ghost" in {d.person_id for d in docs}


async def test_fetch_skips_an_orphan_directory(tmp_path: Path) -> None:
    _tree(tmp_path)
    _write(tmp_path / "people/ghost/blog/only.md", "# Ghost\n\nAn orphan post here.\n")

    async def persons() -> set[str]:
        return {"alice", "bob"}

    src = FilesSource(tmp_path, persons=persons)
    assert await src.fetch("blog/only.md") is None

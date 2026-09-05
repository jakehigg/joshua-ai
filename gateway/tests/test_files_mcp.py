"""The files MCP: path confinement and the six tools, both as units and through
the gateway.

The unit tests drive ``paths.resolve`` against a tmp ``/data`` tree with two
persons. The integration tests host the ``files`` builtin in a gateway app and call
the tools over MCP, so the person hand-off, the tool filter, and the call log run
the same as for an external upstream.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from conftest import bearer, gateway_session
from joshua_gateway.files_mcp import paths, server
from joshua_gateway.main import lifespan
from joshua_gateway.observability import CALL_LOG
from joshua_shared import wikigit

# A 1x1 PNG, enough to prove an image read returns an ImageContent block.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def make_pdf(texts: list[str]) -> bytes:
    """Build a minimal multi-page PDF; each page shows one line of ``texts``."""
    count = len(texts)
    page_ids = [4 + 2 * i for i in range(count)]

    def obj(num: int, body: str) -> str:
        return f"{num} 0 obj\n{body}\nendobj\n"

    bodies = {
        1: obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
        2: obj(
            2,
            f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {count} >>",
        ),
        3: obj(3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    }
    for i, text in enumerate(texts):
        pid, cid = 4 + 2 * i, 5 + 2 * i
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
        bodies[pid] = obj(
            pid,
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 3 0 R >> >> >>",
        )
        bodies[cid] = obj(cid, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")

    max_id = 3 + 2 * count
    out = "%PDF-1.4\n"
    offsets = {}
    for num in range(1, max_id + 1):
        offsets[num] = len(out.encode("latin-1"))
        out += bodies[num]
    xref_pos = len(out.encode("latin-1"))
    out += f"xref\n0 {max_id + 1}\n0000000000 65535 f \n"
    for num in range(1, max_id + 1):
        out += f"{offsets[num]:010d} 00000 n \n"
    out += f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    return out.encode("latin-1")


@pytest.fixture
def fixed_clock(monkeypatch):
    """Freeze the journal clock at 2026-08-27 18:32:10 UTC (14:32 in America/New_York)."""
    instant = datetime(2026, 8, 27, 18, 32, 10, tzinfo=UTC)
    monkeypatch.setattr(server, "_now", lambda: instant)
    return instant


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A tmp ``/data`` tree with two persons, wired through ``JOSHUA_DATA_DIR``."""
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "people" / "alex" / "attachments" / "2026" / "08").mkdir(parents=True)
    (root / "people" / "mia").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "wiki" / "note.md").write_text("hello wiki\ntodo item\n")
    (root / "shared" / "recipes.md").write_text("shared pizza\n")
    monkeypatch.setenv(server.DATA_ENV, str(root))
    return root


def files_yaml() -> str:
    return textwrap.dedent("""\
        files:
          kind: builtin
          allow: all
        """)


def result_json(res):
    return json.loads(res.content[0].text)


# -- paths.resolve unit (property-style traversal) --------------------------


def roots(data_root, wiki_write=True):
    """The root set of a request. The role alone decides what is reachable."""
    return paths.roots(data_root, role="member" if wiki_write else "guest")


TRAVERSALS = [
    "../secret.md",
    "wiki/../../etc/passwd",
    "/data/people/mia/wiki/x.md",
    "wiki/../../../etc/passwd",
    "..",
    "wiki/sub/../../people/x.md",
]


@pytest.mark.parametrize("bad", TRAVERSALS)
def test_resolve_rejects_traversal(data_root, bad):
    with pytest.raises(paths.PathError):
        paths.resolve(bad, roots(data_root), write=False)


@pytest.mark.parametrize("bad", TRAVERSALS)
def test_traversal_message_hides_absolute_path(data_root, bad):
    try:
        paths.resolve(bad, roots(data_root), write=False)
    except paths.PathError as exc:
        assert str(data_root) not in exc.message


def test_resolve_rejects_write_to_people(data_root):
    """``people`` is read-only through this server, whatever the role."""
    with pytest.raises(paths.PathError, match="read-only"):
        paths.resolve("people/alex/attachments/2026/08/a.md", roots(data_root), write=True)


def test_resolve_rejects_non_md_write(data_root):
    with pytest.raises(paths.PathError):
        paths.resolve("wiki/script.py", roots(data_root), write=True)


def test_resolve_allows_wiki_md_write(data_root):
    root, abs_path = paths.resolve("wiki/new.md", roots(data_root), write=True)
    assert root.name == "wiki"
    assert abs_path == data_root / "wiki" / "new.md"


def test_resolve_rejects_symlink_escape(data_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("leak\n")
    (data_root / "wiki" / "evil").symlink_to(outside)
    with pytest.raises(paths.PathError):
        paths.resolve("wiki/evil/secret.md", roots(data_root), write=False)


@pytest.mark.parametrize(
    "hidden",
    [
        "wiki/.git/config",
        "wiki/.trash/2026/x.md",
        "people/alex/attachments/.trash/old.md",
        "shared/.obsidian/workspace.md",
    ],
)
def test_resolve_rejects_a_hidden_entry(data_root, hidden):
    """A dot segment, anywhere below the root, is forbidden — not only at the
    top: it is a frontend's own state, never content."""
    with pytest.raises(paths.PathError):
        paths.resolve(hidden, roots(data_root), write=False)


def test_hidden_entry_message_hides_absolute_path(data_root):
    try:
        paths.resolve("wiki/.git/config", roots(data_root), write=False)
    except paths.PathError as exc:
        assert str(data_root) not in exc.message


def test_wiki_write_gated_by_role(data_root):
    with pytest.raises(paths.PathError):
        paths.resolve("wiki/x.md", roots(data_root, wiki_write=False), write=True)
    root, _ = paths.resolve("wiki/x.md", roots(data_root, wiki_write=True), write=True)
    assert root.name == "wiki"


def test_shared_is_never_writable(data_root):
    with pytest.raises(paths.PathError):
        paths.resolve("shared/x.md", roots(data_root, wiki_write=True), write=True)


def test_a_request_with_no_role_writes_nothing(data_root):
    """The safe default: a turn core could not attribute reads and writes nothing."""
    for role in ("", "guest", "nonsense"):
        r = paths.roots(data_root, role=role)
        assert set(r) == {"wiki", "people", "shared"}
        assert all(root.can_write is False for root in r.values())


def test_a_member_writes_the_wiki_only(data_root):
    """``people`` and ``shared`` are read-only through this server for every role."""
    r = roots(data_root)
    assert r["wiki"].can_write is True
    assert r["people"].can_write is False
    assert r["shared"].can_write is False


def test_a_member_reads_the_attachments_of_another_person(data_root):
    """One corpus. Another person's attachments are not walled off from alex."""
    r = roots(data_root)
    root, resolved = paths.resolve("people/mia/attachments/x.jpg", r, write=False)
    assert root.name == "people"
    assert resolved == data_root / "people" / "mia" / "attachments" / "x.jpg"


def test_a_retired_root_names_its_replacement(data_root):
    """A skill can still name a retired top-level root. Hand back the new path."""
    with pytest.raises(paths.PathError, match=r"wiki/people/"):
        paths.resolve("profile.md", roots(data_root), write=False)
    with pytest.raises(paths.PathError, match=r"people/.*/attachments/"):
        paths.resolve("attachments/x.jpg", roots(data_root), write=False)


def test_the_blog_root_has_no_special_message_now(data_root):
    """The journal moved into a tool call, not a path, so there is no single
    replacement path to hand back for the old ``blog`` root."""
    with pytest.raises(paths.PathError, match="unknown or forbidden root"):
        paths.resolve("blog/x.md", roots(data_root), write=False)


# -- through the gateway ----------------------------------------------------


async def test_write_then_read_roundtrip(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            written = await session.call_tool(
                "write_file", {"path": "wiki/recipes/pizza.md", "content": "# Pizza\n"}
            )
            assert written.is_error is False
            assert result_json(written)["bytes"] == len("# Pizza\n")
            read = await session.call_tool("read_file", {"path": "wiki/recipes/pizza.md"})
            assert read.content[0].text == "# Pizza\n"
    assert (data_root / "wiki" / "recipes" / "pizza.md").exists()


async def test_create_twice_fails(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            first = await session.call_tool("write_file", {"path": "wiki/x.md", "content": "one\n"})
            assert first.is_error is False
            again = await session.call_tool("write_file", {"path": "wiki/x.md", "content": "two\n"})
            assert again.is_error is True
            assert "exists" in again.content[0].text


async def test_overwrite_and_append(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            await session.call_tool(
                "write_file", {"path": "wiki/x.md", "content": "one\n", "mode": "overwrite"}
            )
            await session.call_tool(
                "write_file", {"path": "wiki/x.md", "content": "two\n", "mode": "append"}
            )
            read = await session.call_tool("read_file", {"path": "wiki/x.md"})
    assert read.content[0].text == "one\ntwo\n"


async def test_invalid_frontmatter_rejected(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            bad = await session.call_tool(
                "write_file",
                {"path": "wiki/fm.md", "content": "---\nnot: [valid\n---\nbody\n"},
            )
            assert bad.is_error is True
            good = await session.call_tool(
                "write_file",
                {"path": "wiki/ok.md", "content": "---\ntitle: Ok\n---\nbody\n"},
            )
            assert good.is_error is False


async def test_write_rejects_over_256kb(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            big = await session.call_tool(
                "write_file", {"path": "wiki/big.md", "content": "x" * (256 * 1024 + 1)}
            )
    assert big.is_error is True


@pytest.mark.parametrize(
    "args",
    [
        {"path": "people/alex/attachments/2026/08/a.md", "content": "x\n"},
        {"path": "wiki/script.py", "content": "print(1)\n"},
        {"path": "../escape.md", "content": "x\n"},
        {"path": "/data/people/mia/wiki/x.md", "content": "x\n"},
    ],
)
async def test_write_rejections_hide_absolute_path(gateway, data_root, args):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("write_file", args)
    assert res.is_error is True
    assert str(data_root) not in res.content[0].text


async def test_a_write_to_people_is_refused_for_a_member_now(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file",
                {"path": "people/alex/attachments/2026/08/note.md", "content": "x\n"},
            )
    assert res.is_error is True and "read-only" in res.content[0].text
    assert not (data_root / "people" / "alex" / "attachments" / "2026" / "08" / "note.md").exists()


async def test_list_and_search(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            listing = await session.call_tool("list_files", {"root": "wiki"})
            paths_seen = [e["path"] for e in result_json(listing)]
            assert "wiki/note.md" in paths_seen
            found = await session.call_tool("search_files", {"query": "todo", "root": "wiki"})
            hits = result_json(found)
    assert hits and hits[0]["path"] == "wiki/note.md" and hits[0]["line"] == 2


async def test_list_and_search_ignore_dot_entries(gateway, data_root):
    """A wiki frontend's own state (``.git``, ``.obsidian``, and so on), at any
    depth, never shows up in a listing or a search — only ``.trash`` was hidden
    before; now every dot entry is."""
    (data_root / "wiki" / ".git" / "refs").mkdir(parents=True)
    (data_root / "wiki" / ".git" / "config").write_text("not markdown\n")
    (data_root / "wiki" / ".git" / "refs" / "todo.md").write_text("todo git internals\n")
    (data_root / "wiki" / ".obsidian").mkdir()
    (data_root / "wiki" / ".obsidian" / "todo.md").write_text("todo obsidian state\n")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            flat = result_json(await session.call_tool("list_files", {"root": "wiki"}))
            recursive = result_json(
                await session.call_tool("list_files", {"root": "wiki", "recursive": True})
            )
            found = await session.call_tool("search_files", {"query": "todo", "root": "wiki"})
    hits = result_json(found)
    assert {e["path"] for e in flat} == {"wiki/note.md"}
    assert {e["path"] for e in recursive} == {"wiki/note.md"}
    assert {h["path"] for h in hits} == {"wiki/note.md"}


async def test_read_image_returns_image_block(gateway, data_root):
    (data_root / "people" / "alex" / "attachments" / "2026" / "08" / "p.png").write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/p.png"}
            )
    block = res.content[0]
    assert block.type == "image"
    assert block.mime_type == "image/png"
    assert base64.b64decode(block.data) == PNG_1PX


async def test_read_heic_returns_metadata_only(gateway, data_root):
    heic = data_root / "people" / "alex" / "attachments" / "2026" / "08" / "a.heic"
    # Real HEIC magic plus a 0xff byte, which is never valid UTF-8.
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic\xff\xd8not-an-image")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/a.heic"}
            )
    payload = result_json(res)
    assert payload["path"] == "people/alex/attachments/2026/08/a.heic"
    assert "note" in payload


async def test_read_attachment_allowed_write_people_denied(gateway, data_root):
    (data_root / "people" / "alex" / "attachments" / "2026" / "08" / "p.png").write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            read = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/p.png"}
            )
            write = await session.call_tool(
                "write_file",
                {"path": "people/alex/attachments/2026/08/new.md", "content": "x\n"},
            )
    assert read.is_error is False
    assert write.is_error is True and "read-only" in write.content[0].text


async def test_wiki_is_one_for_everyone(gateway, data_root):
    """Alex writes a page; Mia reads the same page. There is one wiki."""
    app = gateway(files_yaml())
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", {"X-Joshua-Person": "alex"}) as session:
            await session.call_tool("write_file", {"path": "wiki/mine.md", "content": "j\n"})
        async with gateway_session(app, "/files", "core", {"X-Joshua-Person": "mia"}) as session:
            seen = await session.call_tool("read_file", {"path": "wiki/mine.md"})
    assert (data_root / "wiki" / "mine.md").exists()
    assert seen.is_error is False
    assert seen.content[0].text == "j\n"


async def test_member_teaches_skill_guest_cannot(gateway, data_root, config_path, monkeypatch):
    """A taught skill is a wiki file: a member writes ``wiki/skills/<slug>.md``;
    a guest write to the same path is refused and the file is not created."""
    from joshua_shared import config

    app = gateway(files_yaml())
    text = config_path.read_text().replace("    name: Mia\n", "    name: Mia\n    role: guest\n")
    config_path.write_text(text)
    monkeypatch.setattr(config, "_cache", None)
    skill = '---\nname: movie time\ntriggers: ["movie time"]\n---\nDim the lights.\n'
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", {"X-Joshua-Person": "alex"}) as session:
            member = await session.call_tool(
                "write_file", {"path": "wiki/skills/movie.md", "content": skill}
            )
        async with gateway_session(app, "/files", "core", {"X-Joshua-Person": "mia"}) as session:
            guest = await session.call_tool(
                "write_file", {"path": "wiki/skills/guest.md", "content": skill}
            )
    assert member.is_error is False
    assert (data_root / "wiki" / "skills" / "movie.md").exists()
    assert guest.is_error is True and "read-only" in guest.content[0].text
    assert not (data_root / "wiki" / "skills" / "guest.md").exists()


async def test_guest_reads_wiki_but_cannot_write_it(gateway, data_root, config_path, monkeypatch):
    from joshua_shared import config

    app = gateway(files_yaml())
    text = config_path.read_text().replace("    name: Mia\n", "    name: Mia\n    role: guest\n")
    assert "role: guest" in text
    config_path.write_text(text)
    monkeypatch.setattr(config, "_cache", None)
    async with lifespan(app):
        headers = {"X-Joshua-Person": "mia"}
        async with gateway_session(app, "/files", "core", headers) as session:
            read = await session.call_tool("read_file", {"path": "wiki/note.md"})
            write = await session.call_tool("write_file", {"path": "wiki/g.md", "content": "g\n"})
            rename = await session.call_tool(
                "rename_file", {"path": "wiki/note.md", "new_name": "n2.md"}
            )
            journal = await session.call_tool(
                "write_journal_entry", {"slug": "today", "markdown": "ok\n"}
            )
    assert read.content[0].text == "hello wiki\ntodo item\n"
    assert write.is_error is True and "read-only" in write.content[0].text
    assert rename.is_error is True
    # A guest writes nothing, the journal included.
    assert journal.is_error is True and "read-only" in journal.content[0].text
    assert not (data_root / "wiki" / "g.md").exists()


async def test_no_role_reads_the_corpus_and_writes_nothing(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        async with gateway_session(app, "/files", "core") as session:
            shared = await session.call_tool("read_file", {"path": "shared/recipes.md"})
            assert shared.content[0].text == "shared pizza\n"
            listed = await session.call_tool("list_files", {"root": "wiki"})
            denied = await session.call_tool("write_file", {"path": "wiki/x.md", "content": "x\n"})
            people = await session.call_tool("list_files", {"root": "people"})
    assert listed.is_error is False
    assert "wiki/note.md" in [i["path"] for i in result_json(listed)]
    assert denied.is_error is True
    # One corpus: a request with no role reads people/, and writes nothing.
    assert people.is_error is False


async def test_shared_is_read_only_for_a_member(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file", {"path": "shared/team.md", "content": "team\n"}
            )
    assert res.is_error is True
    assert not (data_root / "shared" / "team.md").exists()


async def test_call_log_records_files_and_person(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            await session.call_tool("list_files", {"root": "wiki"})
    entry = next(c for c in CALL_LOG.recent() if c["tool"] == "list_files")
    assert entry["server"] == "files"
    assert entry["person"] == "alex"


async def test_inventory_shows_files_builtin(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/admin/inventory", headers=bearer("laptop"))
    files = resp.json()["servers"]["files"]
    assert files["kind"] == "builtin"
    assert files["status"] == "connected"
    assert sorted(files["tools"]) == [
        "list_files",
        "read_file",
        "rename_file",
        "search_files",
        "write_file",
        "write_journal_entry",
    ]


def test_build_builtin_server_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown builtin"):
        server.build_builtin_server("nope", {})


# -- write_journal_entry -----------------------------------------------------


async def test_journal_entry_lands_with_frontmatter(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry",
                {
                    "slug": "alex-breakfast",
                    "markdown": "Alex had oatmeal.\n",
                    "people": ["alex"],
                },
            )
    assert res.is_error is False
    payload = result_json(res)
    assert payload["path"] == "wiki/journal/2026/08/27/alex-breakfast.md"
    assert payload["overwritten"] is False
    entry = data_root / "wiki" / "journal" / "2026" / "08" / "27" / "alex-breakfast.md"
    text = entry.read_text()
    assert text.startswith("---\n")
    assert "date: '2026-08-27'" in text
    assert "people:\n- alex" in text
    assert "source: agent" in text
    assert "Alex had oatmeal." in text


async def test_journal_entry_defaults_to_no_people(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry", {"slug": "quiet-evening", "markdown": "Nothing much.\n"}
            )
    assert res.is_error is False
    entry = data_root / "wiki" / "journal" / "2026" / "08" / "27" / "quiet-evening.md"
    assert "people: []" in entry.read_text()


async def test_journal_entry_custom_date(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry",
                {"slug": "trip", "markdown": "x\n", "date": "2026-01-05"},
            )
    assert result_json(res)["path"] == "wiki/journal/2026/01/05/trip.md"
    assert (data_root / "wiki" / "journal" / "2026" / "01" / "05" / "trip.md").is_file()


async def test_journal_entry_bad_date_refused(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry", {"slug": "trip", "markdown": "x\n", "date": "not-a-date"}
            )
    assert res.is_error is True and "date" in res.content[0].text


async def test_journal_entry_people_must_be_a_list(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry", {"slug": "trip", "markdown": "x\n", "people": "alex"}
            )
    assert res.is_error is True and "people" in res.content[0].text


async def test_journal_entry_overwrite_says_so(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            first = await session.call_tool(
                "write_journal_entry", {"slug": "garden", "markdown": "one\n"}
            )
            second = await session.call_tool(
                "write_journal_entry", {"slug": "garden", "markdown": "two\n"}
            )
    assert result_json(first)["overwritten"] is False
    assert result_json(second)["overwritten"] is True
    entry = data_root / "wiki" / "journal" / "2026" / "08" / "27" / "garden.md"
    text = entry.read_text()
    assert "two" in text
    assert "one" not in text


async def test_write_file_refuses_the_journal(gateway, data_root, fixed_clock):
    """The journal has one writer. A free write would skip the day folder and
    the frontmatter the index reads."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file",
                {"path": "wiki/journal/2026/08/27/sneaky.md", "content": "no frontmatter\n"},
            )
    assert res.is_error is True and "write_journal_entry" in res.content[0].text
    assert not (data_root / "wiki" / "journal" / "2026" / "08" / "27" / "sneaky.md").exists()


async def test_write_file_cannot_overwrite_the_nightly_page(gateway, data_root, fixed_clock):
    """The nightly page is core's. The agent must not be able to replace it."""
    page = data_root / "wiki" / "journal" / "2026" / "08" / "27" / "2026-08-27.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ndate: '2026-08-27'\npeople: []\nsource: nightly\n---\n\nThe day.\n")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file",
                {
                    "path": "wiki/journal/2026/08/27/2026-08-27.md",
                    "content": "clobbered\n",
                    "mode": "overwrite",
                },
            )
    assert res.is_error is True and "write_journal_entry" in res.content[0].text
    assert "The day." in page.read_text()


async def test_rename_file_refuses_a_journal_entry(gateway, data_root, fixed_clock):
    """A rename would move an entry out of the slug its day folder indexes."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            await session.call_tool(
                "write_journal_entry", {"slug": "garden", "markdown": "Planted beans.\n"}
            )
            res = await session.call_tool(
                "rename_file",
                {"path": "wiki/journal/2026/08/27/garden.md", "new_name": "beans.md"},
            )
    assert res.is_error is True and "cannot be renamed" in res.content[0].text
    day = data_root / "wiki" / "journal" / "2026" / "08" / "27"
    assert (day / "garden.md").exists() and not (day / "beans.md").exists()


async def test_journal_entry_guest_refused(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "guest"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry", {"slug": "no-access", "markdown": "nope\n"}
            )
    assert res.is_error is True and "read-only" in res.content[0].text
    assert not (data_root / "wiki" / "journal" / "2026" / "08" / "27" / "no-access.md").exists()


async def test_journal_entry_bad_slug_refused(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_journal_entry", {"slug": "Not A Slug!", "markdown": "x\n"}
            )
    assert res.is_error is True
    assert "invalid journal slug" in res.content[0].text
    assert str(data_root) not in res.content[0].text


@pytest.mark.parametrize("slug", ["..", ".hidden", "../escape"])
async def test_journal_entry_traversal_slug_refused(gateway, data_root, fixed_clock, slug):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("write_journal_entry", {"slug": slug, "markdown": "x\n"})
    assert res.is_error is True
    assert str(data_root) not in res.content[0].text


# -- rename_file --------------------------------------------------------------


async def test_rename_wiki_file(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "rename_file", {"path": "wiki/note.md", "new_name": "todo.md"}
            )
    assert result_json(res)["path"] == "wiki/todo.md"
    assert (data_root / "wiki" / "todo.md").exists()


async def test_rename_in_people_is_refused(gateway, data_root):
    """The write domain shrank to ``wiki/`` only; an attachment can no longer be
    renamed through this server."""
    src = data_root / "people" / "alex" / "attachments" / "2026" / "08" / "IMG_4471.jpg"
    src.write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "rename_file",
                {
                    "path": "people/alex/attachments/2026/08/IMG_4471.jpg",
                    "new_name": "garden-north-bed.jpg",
                },
            )
    assert res.is_error is True
    assert src.exists()


@pytest.mark.parametrize(
    "args",
    [
        {"path": "wiki/note.md", "new_name": "../x.md"},
        {"path": "wiki/note.md", "new_name": "sub/x.md"},
        {"path": "/data/people/mia/wiki/x.md", "new_name": "y.md"},
        {"path": "wiki/note.md", "new_name": "note.py"},
        {"path": "people/alex/attachments/2026/08/a.jpg", "new_name": "other.jpg"},
    ],
)
async def test_rename_rejections_hide_absolute_path(gateway, data_root, args):
    (data_root / "people" / "alex" / "attachments" / "2026" / "08" / "a.jpg").write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("rename_file", args)
    assert res.is_error is True
    assert str(data_root) not in res.content[0].text


async def test_rename_refuses_existing_target(gateway, data_root):
    (data_root / "wiki" / "a.md").write_text("a\n")
    (data_root / "wiki" / "b.md").write_text("b\n")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("rename_file", {"path": "wiki/a.md", "new_name": "b.md"})
    assert res.is_error is True and "exists" in res.content[0].text


# -- original_name and PDF read ---------------------------------------------


async def test_list_files_adds_original_name(gateway, data_root):
    base = data_root / "people" / "alex" / "attachments" / "2026" / "08"
    stored = base / "2026-08-27-143210-IMG_4471.jpg"
    stored.write_bytes(PNG_1PX)
    (base / "2026-08-27-143210-IMG_4471.jpg.meta.json").write_text(
        json.dumps({"original_name": "IMG_4471.HEIC", "mime": "image/jpeg"})
    )
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            listing = await session.call_tool("list_files", {"root": "people", "recursive": True})
    entries = result_json(listing)
    paths_seen = [e["path"] for e in entries]
    assert not any(p.endswith(".meta.json") for p in paths_seen)
    entry = next(e for e in entries if e["path"].endswith("IMG_4471.jpg"))
    assert entry["original_name"] == "IMG_4471.HEIC"


async def test_list_files_reports_the_attachment_metadata(gateway, data_root):
    """``list_files`` carries the sender's filename and hides the metadata file itself.

    The metadata file is written here in the shape ``docs/data-layout.md`` documents,
    and not through the channels pipeline: a gateway test must not import
    another container. ``channels`` pins the writer side in
    ``test_metadata_holds_original_name_mime_and_received_at``.
    """
    base = data_root / "people" / "alex" / "attachments" / "2026" / "08"
    stored = base / "2026-08-27-143210-IMG_0001.png"
    stored.write_bytes(PNG_1PX)
    (base / f"{stored.name}.meta.json").write_text(
        json.dumps(
            {
                "original_name": "IMG_0001.png",
                "mime": "image/png",
                "received_at": "2026-08-27T14:32:10+00:00",
            }
        ),
        encoding="utf-8",
    )

    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            listing = await session.call_tool("list_files", {"root": "people", "recursive": True})
    entries = result_json(listing)
    assert not any(e["path"].endswith(".meta.json") for e in entries)
    entry = next(e for e in entries if e["path"].endswith(stored.name))
    assert entry["original_name"] == "IMG_0001.png"


async def test_list_files_includes_attachment_without_metadata(gateway, data_root):
    # A missing or failed metadata file degrades to "no original_name", never to a
    # stored file that drops out of the listing. This locks the graceful path:
    # the bytes are the product; the metadata file is optional metadata.
    base = data_root / "people" / "alex" / "attachments" / "2026" / "08"
    stored = base / "2026-08-27-143210-IMG_9999.jpg"
    stored.write_bytes(PNG_1PX)  # no .meta.json metadata file next to it
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            listing = await session.call_tool("list_files", {"root": "people", "recursive": True})
    entries = result_json(listing)
    entry = next(e for e in entries if e["path"].endswith("IMG_9999.jpg"))
    assert "original_name" not in entry


async def test_read_pdf_returns_text(gateway, data_root):
    pdf = data_root / "people" / "alex" / "attachments" / "2026" / "08" / "doc.pdf"
    pdf.write_bytes(make_pdf(["Hello PDF"]))
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/doc.pdf"}
            )
    text = res.content[0].text
    assert "PDF people/alex/attachments/2026/08/doc.pdf: 1 page(s)" in text
    assert "Hello PDF" in text


async def test_read_pdf_caps_long_document(gateway, data_root):
    pdf = data_root / "people" / "alex" / "attachments" / "2026" / "08" / "big.pdf"
    pdf.write_bytes(make_pdf([f"Page {i + 1}" for i in range(30)]))
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/big.pdf"}
            )
    text = res.content[0].text
    assert "30 page(s)" in text
    assert "capped" in text
    assert "Page 1" in text and "Page 20" in text
    assert "Page 21" not in text


async def test_read_corrupt_pdf_falls_back_to_metadata(gateway, data_root):
    pdf = data_root / "people" / "alex" / "attachments" / "2026" / "08" / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnot a real pdf\n")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "read_file", {"path": "people/alex/attachments/2026/08/broken.pdf"}
            )
    payload = result_json(res)
    assert payload["path"] == "people/alex/attachments/2026/08/broken.pdf"
    assert "PDF" in payload["note"]


# -- The role is the boundary --------------------------------------------------


async def test_the_role_header_grants_the_write(gateway, data_root):
    """A group turn carries no person and still writes, because it carries a role."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "member"}
        async with gateway_session(app, "/files", "core", headers) as session:
            wrote = await session.call_tool(
                "write_file", {"path": "wiki/group.md", "content": "from the family chat\n"}
            )
    assert wrote.is_error is False
    assert (data_root / "wiki" / "group.md").exists()


async def test_the_role_header_refuses_the_write_for_a_guest(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "guest"}
        async with gateway_session(app, "/files", "core", headers) as session:
            wrote = await session.call_tool(
                "write_file", {"path": "wiki/nope.md", "content": "no\n"}
            )
            read = await session.call_tool("read_file", {"path": "wiki/note.md"})
    assert wrote.is_error is True and "read-only" in wrote.content[0].text
    # A guest still reads the whole corpus.
    assert read.is_error is False
    assert not (data_root / "wiki" / "nope.md").exists()


async def test_an_unknown_role_is_refused_at_the_boundary(gateway):
    """The gateway validates what core asserts; a bad role is a 400, not a guess."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex", "X-Joshua-Role": "root"}
        with pytest.raises(Exception):  # noqa: B017 — the transport surfaces the 400
            async with gateway_session(app, "/files", "core", headers) as session:
                await session.call_tool("read_file", {"path": "wiki/note.md"})


# -- The wiki commit hook ----------------------------------------------------

_GIT_MISSING = shutil.which("git") is None


def _git_log(wiki: Path) -> str:
    """The one-line log, or "" when the repository has no commit yet (or the
    call fails for any other reason)."""
    result = subprocess.run(["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
async def test_a_member_write_produces_a_commit_naming_the_path(gateway, data_root):
    wikigit.ensure_repo(data_root / "wiki")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            await session.call_tool(
                "write_file", {"path": "wiki/recipes/pizza.md", "content": "# Pizza\n"}
            )
    log = _git_log(data_root / "wiki")
    assert "write_file: recipes/pizza.md" in log


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
async def test_a_guests_refused_write_produces_no_commit(gateway, data_root):
    wikigit.ensure_repo(data_root / "wiki")
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "guest"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("write_file", {"path": "wiki/nope.md", "content": "no\n"})
    assert res.is_error is True
    assert _git_log(data_root / "wiki") == ""


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
async def test_wiki_git_false_produces_no_commit(gateway, data_root):
    wikigit.ensure_repo(data_root / "wiki")
    app = gateway(files_yaml(), wiki_git=False)
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            written = await session.call_tool("write_file", {"path": "wiki/x.md", "content": "x\n"})
    assert written.is_error is False
    assert (data_root / "wiki" / "x.md").exists()
    assert _git_log(data_root / "wiki") == ""


async def test_a_failing_git_commit_does_not_fail_the_write_tool(gateway, data_root, monkeypatch):
    """`wikigit.commit` never raises for real, but the call site must not
    trust that: a monkeypatched raise must still leave the write intact."""

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(wikigit, "commit", _boom)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool("write_file", {"path": "wiki/ok.md", "content": "ok\n"})
    assert res.is_error is False
    assert (data_root / "wiki" / "ok.md").read_text() == "ok\n"

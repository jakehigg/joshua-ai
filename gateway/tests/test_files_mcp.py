"""The files MCP: path confinement and the four tools, both as units and through
the gateway.

The unit tests drive ``paths.resolve`` against a tmp ``/data`` tree with two
persons. The integration tests host the ``files`` builtin in a gateway app and call
the tools over MCP, so the person hand-off, the tool filter, and the call log run
the same as for an external upstream.
"""

from __future__ import annotations

import base64
import json
import textwrap
from datetime import UTC, datetime

import httpx2
import pytest
from conftest import bearer, gateway_session
from joshua_gateway.files_mcp import paths, server
from joshua_gateway.main import lifespan
from joshua_gateway.observability import CALL_LOG

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
    """Freeze the blog clock at 2026-08-27 18:32:10 UTC (14:32 in America/New_York)."""
    instant = datetime(2026, 8, 27, 18, 32, 10, tzinfo=UTC)
    monkeypatch.setattr(server, "_now", lambda: instant)
    return instant


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A tmp ``/data`` tree with two persons, wired through ``JOSHUA_DATA_DIR``."""
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "people" / "alex" / "blog").mkdir(parents=True)
    (root / "people" / "mia" / "blog").mkdir(parents=True)
    (root / "people" / "alex" / "attachments" / "2026" / "08").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "people" / "alex" / "profile.md").write_text("# Alex\n")
    (root / "people" / "alex" / "blog" / "2026-08-24.md").write_text("dear diary\n")
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


ROSTER = ("alex", "mia")


def roots(data_root, person="alex", wiki_write=True):
    """The root set of a request. The role decides what is reachable.

    ``person`` says who the request belongs to. It widens nothing; it decides
    only whether a write below ``people`` has a person segment to land on.
    """
    return paths.roots(
        data_root,
        role="member" if wiki_write else "guest",
        people=ROSTER,
        person=person,
    )


TRAVERSALS = [
    "../secret.md",
    "wiki/../../etc/passwd",
    "/data/people/mia/wiki/x.md",
    "wiki/../../../etc/passwd",
    "..",
    "wiki/sub/../../blog/x.md",
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


def test_resolve_allows_blog_md_write(data_root):
    root, abs_path = paths.resolve(
        "people/alex/blog/2026-08-24-1200-note.md", roots(data_root), write=True
    )
    assert root.name == "people"
    assert abs_path == data_root / "people" / "alex" / "blog" / "2026-08-24-1200-note.md"


def test_resolve_rejects_non_md_blog_write(data_root):
    with pytest.raises(paths.PathError):
        paths.resolve("people/alex/blog/note.txt", roots(data_root), write=True)


def test_resolve_rejects_write_to_attachments(data_root):
    with pytest.raises(paths.PathError):
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
        "people/alex/blog/.trash/old.md",
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


def test_a_member_writes_the_wiki_and_the_journal(data_root):
    r = roots(data_root)
    assert r["wiki"].can_write is True
    assert r["people"].can_write is True
    assert r["shared"].can_write is False


def test_reading_does_not_depend_on_a_person(data_root):
    """Identity is a path lookup, and not a read permission."""
    alex = paths.roots(data_root, role="member", people=ROSTER, person="alex")
    mia = paths.roots(data_root, role="member", people=ROSTER, person="mia")
    for name in ("wiki", "people", "shared"):
        assert alex[name].base == mia[name].base
        assert alex[name].can_write == mia[name].can_write


def test_a_member_reads_the_journal_of_another_person(data_root):
    """One corpus. The journal of mia is not walled off from alex."""
    r = roots(data_root)
    root, resolved = paths.resolve("people/mia/blog/x.md", r, write=False)
    assert root.name == "people"
    assert resolved == data_root / "people" / "mia" / "blog" / "x.md"


def test_the_write_domain_holds_below_people(data_root):
    """The role says whether a write may happen; this says where it may land."""
    r = roots(data_root)
    paths.resolve("people/alex/blog/ok.md", r, write=True)
    for denied in ("people/alex/profile.md", "people/alex/attachments/x.md"):
        with pytest.raises(paths.PathError, match="may be written"):
            paths.resolve(denied, r, write=True)


def test_a_retired_root_names_its_replacement(data_root):
    """A skill can still name a retired root. Hand back the whole path.

    The replacement carries the person id of the request, so the caller never
    composes the segment itself.
    """
    with pytest.raises(paths.PathError, match=r"people/alex/blog/"):
        paths.resolve("blog/x.md", roots(data_root), write=True)


def test_a_retired_root_asks_for_no_guess_with_no_person(data_root):
    """With no person there is no id to name, and none is invented."""
    r = roots(data_root, person=None)
    with pytest.raises(paths.PathError) as excinfo:
        paths.resolve("blog/x.md", r, write=True)
    assert "<person>" not in excinfo.value.message


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
        {"path": "people/alex/blog/2026-08-24.md", "content": "x\n", "mode": "overwrite"},
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


async def test_read_blog_allowed_write_blog_denied(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            read = await session.call_tool("read_file", {"path": "people/alex/blog/2026-08-24.md"})
            assert read.content[0].text == "dear diary\n"
            write = await session.call_tool(
                "write_file",
                {"path": "people/alex/blog/x.md", "content": "x\n", "mode": "overwrite"},
            )
    assert write.is_error is True


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
            blog = await session.call_tool(
                "write_file", {"path": "people/alex/blog/today.md", "content": "ok\n"}
            )
    assert read.content[0].text == "hello wiki\ntodo item\n"
    assert write.is_error is True and "read-only" in write.content[0].text
    assert rename.is_error is True
    # A guest writes nothing, the journal included.
    assert blog.is_error is True and "read-only" in blog.content[0].text
    assert not (data_root / "wiki" / "g.md").exists()


async def test_read_profile_allowed_write_denied(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            read = await session.call_tool("read_file", {"path": "people/alex/profile.md"})
            assert read.content[0].text == "# Alex\n"
            write = await session.call_tool(
                "write_file",
                {"path": "people/alex/profile.md", "content": "x\n", "mode": "overwrite"},
            )
    assert write.is_error is True


async def test_no_role_reads_the_corpus_and_writes_nothing(gateway, data_root):
    app = gateway(files_yaml())
    async with lifespan(app):
        async with gateway_session(app, "/files", "core") as session:
            shared = await session.call_tool("read_file", {"path": "shared/recipes.md"})
            assert shared.content[0].text == "shared pizza\n"
            listed = await session.call_tool("list_files", {"root": "wiki"})
            denied = await session.call_tool("write_file", {"path": "wiki/x.md", "content": "x\n"})
            blog = await session.call_tool("list_files", {"root": "people"})
    assert listed.is_error is False
    assert "wiki/note.md" in [i["path"] for i in result_json(listed)]
    assert denied.is_error is True
    # One corpus: a request with no role reads people/, and writes nothing.
    assert blog.is_error is False


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
    ]


def test_build_builtin_server_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown builtin"):
        server.build_builtin_server("nope", {})


# -- blog write -------------------------------------------------------------


async def test_blog_write_stamps_name_and_frontmatter(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file", {"path": "people/alex/blog/new-fertilizer.md", "content": "# Notes\n"}
            )
    assert res.is_error is False
    assert result_json(res)["path"] == "people/alex/blog/2026-08-27-1432-new-fertilizer.md"
    post = data_root / "people" / "alex" / "blog" / "2026-08-27-1432-new-fertilizer.md"
    text = post.read_text()
    assert text.startswith("---\n")
    assert "person: alex" in text and "source: chat" in text and "attachments: []" in text
    assert "# Notes" in text


async def test_blog_same_minute_slug_dedupes(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            await session.call_tool(
                "write_file", {"path": "people/alex/blog/rue.md", "content": "one\n"}
            )
            second = await session.call_tool(
                "write_file", {"path": "people/alex/blog/rue.md", "content": "two\n"}
            )
    assert result_json(second)["path"] == "people/alex/blog/2026-08-27-1432-rue-2.md"


async def test_blog_digest_name_reserved(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "write_file", {"path": "people/alex/blog/2026-08-27.md", "content": "digest\n"}
            )
    assert res.is_error is True
    assert "reserved" in res.content[0].text
    assert str(data_root) not in res.content[0].text


async def test_blog_keeps_supplied_stamp_and_overwrite_denied(gateway, data_root, fixed_clock):
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            kept = await session.call_tool(
                "write_file",
                {
                    "path": "people/alex/blog/2026-08-01-0900-trip.md",
                    "content": "---\ndate: x\n---\nbody\n",
                },
            )
            over = await session.call_tool(
                "write_file",
                {"path": "people/alex/blog/again.md", "content": "x\n", "mode": "overwrite"},
            )
    assert result_json(kept)["path"] == "people/alex/blog/2026-08-01-0900-trip.md"
    assert over.is_error is True and "append-only" in over.content[0].text


async def test_blog_frontmatter_attachments_validated(gateway, data_root, fixed_clock):
    (data_root / "people" / "alex" / "attachments" / "2026" / "08" / "g.jpg").write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            good = await session.call_tool(
                "write_file",
                {
                    "path": "people/alex/blog/garden.md",
                    "content": (
                        "---\nattachments:\n  - people/alex/attachments/2026/08/g.jpg\n---\nhi\n"
                    ),
                },
            )
            bad = await session.call_tool(
                "write_file",
                {
                    "path": "people/alex/blog/garden.md",
                    "content": (
                        "---\nattachments:\n"
                        "  - people/alex/attachments/2026/08/missing.jpg\n---\nhi\n"
                    ),
                },
            )
    assert good.is_error is False
    assert bad.is_error is True and "attachment not found" in bad.content[0].text


# -- rename_file ------------------------------------------------------------


async def test_rename_attachment_keeps_timestamp_prefix(gateway, data_root):
    src = (
        data_root
        / "people"
        / "alex"
        / "attachments"
        / "2026"
        / "08"
        / "2026-08-27-143210-IMG_4471.jpg"
    )
    src.write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "rename_file",
                {
                    "path": "people/alex/attachments/2026/08/2026-08-27-143210-IMG_4471.jpg",
                    "new_name": "garden-north-bed.jpg",
                },
            )
    assert res.is_error is False
    assert (
        result_json(res)["path"]
        == "people/alex/attachments/2026/08/2026-08-27-143210-garden-north-bed.jpg"
    )
    assert (src.parent / "2026-08-27-143210-garden-north-bed.jpg").exists()
    assert not src.exists()


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


@pytest.mark.parametrize(
    "args",
    [
        {"path": "people/alex/attachments/2026/08/a.jpg", "new_name": "../x.jpg"},
        {"path": "people/alex/attachments/2026/08/a.jpg", "new_name": "sub/x.jpg"},
        {"path": "/data/people/mia/wiki/x.md", "new_name": "y.md"},
        {"path": "people/alex/attachments/2026/08/a.jpg", "new_name": "a.png"},
        {"path": "people/alex/profile.md", "new_name": "other.md"},
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
    base = data_root / "people" / "alex" / "attachments" / "2026" / "08"
    (base / "2026-08-27-143210-a.jpg").write_bytes(PNG_1PX)
    (base / "2026-08-27-143210-b.jpg").write_bytes(PNG_1PX)
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex"}
        async with gateway_session(app, "/files", "core", headers) as session:
            res = await session.call_tool(
                "rename_file",
                {
                    "path": "people/alex/attachments/2026/08/2026-08-27-143210-a.jpg",
                    "new_name": "b.jpg",
                },
            )
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


async def test_list_files_reports_the_attachment_sidecar(gateway, data_root):
    """``list_files`` carries the sender's filename and hides the sidecar itself.

    The sidecar is written here in the shape ``docs/data-layout.md`` documents,
    and not through the channels pipeline: a gateway test must not import
    another container. ``channels`` pins the writer side in
    ``test_sidecar_holds_original_name_mime_and_received_at``.
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


async def test_list_files_includes_attachment_without_sidecar(gateway, data_root):
    # A missing or failed sidecar degrades to "no original_name", never to a
    # stored file that drops out of the listing. This locks the graceful path:
    # the bytes are the product; the sidecar is optional metadata.
    base = data_root / "people" / "alex" / "attachments" / "2026" / "08"
    stored = base / "2026-08-27-143210-IMG_9999.jpg"
    stored.write_bytes(PNG_1PX)  # no .meta.json sidecar next to it
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


# -- The person segment must name a person -------------------------------------


def test_a_write_to_a_person_who_does_not_exist_is_refused(data_root):
    """A display name is not a person id, and a path built from one names nobody."""
    with pytest.raises(paths.PathError) as excinfo:
        paths.resolve("people/Alex Smith/blog/note.md", roots(data_root), write=True)
    assert "names nobody" in excinfo.value.message
    assert not (data_root / "people" / "Alex Smith").exists()


def test_the_refusal_names_the_path_that_works(data_root):
    """The caller is handed the whole path, so it composes no segment of its own."""
    with pytest.raises(paths.PathError) as excinfo:
        paths.resolve("people/not-a-person/blog/note.md", roots(data_root), write=True)
    message = excinfo.value.message
    assert "people/alex/blog/" in message
    assert str(data_root) not in message


def test_a_turn_with_no_person_writes_no_journal(data_root):
    """A post belongs to somebody. A turn that names nobody has no author."""
    r = roots(data_root, person=None)
    for segment in ("alex", "mia", "not-a-person"):
        with pytest.raises(paths.PathError):
            paths.resolve(f"people/{segment}/blog/x.md", r, write=True)


def test_the_unknown_person_writes_no_journal(data_root):
    """``unknown`` is the literal core sends for a turn it could not attribute."""
    r = roots(data_root, person=paths.UNKNOWN)
    with pytest.raises(paths.PathError):
        paths.resolve("people/alex/blog/x.md", r, write=True)


def test_a_real_person_still_writes_the_journal(data_root):
    """The rule refuses a stranger and keeps the case that has to work."""
    for person in ("alex", "mia"):
        root, abs_path = paths.resolve(
            f"people/{person}/blog/note.md", roots(data_root, person=person), write=True
        )
        assert abs_path == data_root / "people" / person / "blog" / "note.md"


def test_a_person_who_does_not_exist_is_still_readable(data_root):
    """The rule gates a write. Reading the corpus is unchanged."""
    root, abs_path = paths.resolve("people/ghost/blog/x.md", roots(data_root), write=False)
    assert abs_path == data_root / "people" / "ghost" / "blog" / "x.md"


async def test_a_group_turn_writes_no_journal_through_the_gateway(gateway, data_root):
    """A person-less turn reached a person's namespace and made a directory."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "member"}
        async with gateway_session(app, "/files", "core", headers) as session:
            wrote = await session.call_tool(
                "write_file",
                {"path": "people/alex/blog/from-the-group.md", "content": "note\n"},
            )
    assert wrote.is_error is True
    assert not list((data_root / "people" / "alex" / "blog").glob("*from-the-group*"))


async def test_a_display_name_makes_no_directory_through_the_gateway(gateway, data_root):
    """The whole defect in one test: the second, parallel directory is never made."""
    app = gateway(files_yaml())
    async with lifespan(app):
        headers = {"X-Joshua-Person": "alex", "X-Joshua-Role": "member"}
        async with gateway_session(app, "/files", "core", headers) as session:
            wrote = await session.call_tool(
                "write_file", {"path": "people/Alex/blog/garden.md", "content": "note\n"}
            )
    assert wrote.is_error is True
    assert "people/alex/blog/" in wrote.content[0].text
    assert not (data_root / "people" / "Alex Smith").exists()

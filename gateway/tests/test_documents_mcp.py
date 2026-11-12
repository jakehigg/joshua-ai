"""The documents MCP: each tool, the access rules, the git commits, and the
wiki boundary.

Most tests host the ``documents`` and ``files`` builtins in a gateway app and
call the tools over MCP, so the person and role hand-off runs as in
production. Alex is a member and Mia is a member unless a test makes her a
guest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from conftest import bearer, gateway_session
from joshua_gateway.documents_mcp import server as docs_server
from joshua_gateway.documents_mcp import store
from joshua_gateway.files_mcp import boundary
from joshua_gateway.files_mcp import server as files_server
from joshua_gateway.main import lifespan
from joshua_shared import config, wikigit

_GIT_MISSING = shutil.which("git") is None
needs_git = pytest.mark.skipif(_GIT_MISSING, reason="git binary required")

ALEX = {"X-Joshua-Person": "alex", "X-Joshua-Role": "member"}
MIA = {"X-Joshua-Person": "mia", "X-Joshua-Role": "member"}

INSTANT = datetime(2026, 8, 27, 18, 32, 10, tzinfo=UTC)
STAMP = "2026-08-27T18:32:10+00:00"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(docs_server, "_now", lambda: INSTANT)
    return INSTANT


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "people" / "alex").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv(files_server.DATA_ENV, str(root))
    return root


def both_yaml() -> str:
    return textwrap.dedent("""\
        files:
          kind: builtin
          allow: all
        documents:
          kind: builtin
          allow: all
        """)


def as_json(res):
    return json.loads(res.content[0].text)


def docs(root: Path) -> Path:
    return root / "documents"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def git_log(repo: Path, *args: str) -> str:
    return git(repo, "log", "--format=%s|%an", *args)


def make_guest(config_path, monkeypatch) -> None:
    text = config_path.read_text().replace("    name: Mia\n", "    name: Mia\n    role: guest\n")
    config_path.write_text(text)
    monkeypatch.setattr(config, "_cache", None)


async def call(app, tool: str, args: dict, headers=ALEX, route="/documents"):
    async with gateway_session(app, route, "core", headers) as session:
        return await session.call_tool(tool, args)


# -- write and read -----------------------------------------------------------


async def test_a_new_document_gets_defaults_and_server_fields(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_document",
            {
                "path": "documents/alex/meal-plan.md",
                "body": "# Week\n\nSoup on Monday.\n",
                "title": "Meal plan",
            },
        )
        read = await call(app, "read_document", {"path": "documents/alex/meal-plan.md"})
    assert res.is_error is False, res.content[0].text
    assert as_json(res)["created"] is True
    meta = as_json(read)["frontmatter"]
    assert meta == {
        "title": "Meal plan",
        "kind": "note",
        "status": "draft",
        "format": "markdown",
        "tags": [],
        "author": "alex",
        "created": STAMP,
        "updated": STAMP,
    }
    assert as_json(read)["body"] == "# Week\n\nSoup on Monday.\n"
    text = (docs(data_root) / "alex" / "meal-plan.md").read_text()
    assert text.startswith("---\ntitle: Meal plan\n")


async def test_a_new_document_needs_a_title(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(app, "write_document", {"path": "documents/alex/x.md", "body": "x"})
    assert res.is_error is True and "title is required" in res.content[0].text
    assert not (docs(data_root) / "alex" / "x.md").exists()


async def test_a_caller_cannot_forge_the_server_fields(gateway, data_root, monkeypatch):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "one\n", "title": "P"}
        )
        later = datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(docs_server, "_now", lambda: later)
        forged = await call(
            app,
            "write_document",
            {
                "path": "documents/alex/p.md",
                "body": "two\n",
                "author": "mia",
                "created": "2000-01-01",
                "promoted_to": "wiki/x.md",
            },
        )
        sneaky = await call(
            app,
            "write_document",
            {"path": "documents/alex/q.md", "title": "Q", "body": "---\nauthor: mia\n---\nbody\n"},
        )
        meta = as_json(await call(app, "read_document", {"path": "documents/alex/p.md"}))
    assert forged.is_error is False
    fm = meta["frontmatter"]
    assert fm["author"] == "alex"
    assert fm["created"] == STAMP
    assert fm["updated"] == "2026-08-28T09:00:00+00:00"
    assert "promoted_to" not in fm
    assert meta["body"] == "two\n"
    assert sneaky.is_error is True and "frontmatter" in sneaky.content[0].text


ASSIGNMENT_12 = {**ALEX, "X-Joshua-Conversation": "assignment:12"}


async def call_as(app, identity: str, tool: str, args: dict, headers: dict):
    async with gateway_session(app, "/documents", identity, headers) as session:
        return await session.call_tool(tool, args)


async def test_an_assignment_session_is_the_author(gateway, data_root):
    app = gateway(both_yaml())
    path = "documents/alex/findings.md"
    async with lifespan(app):
        res = await call_as(
            app,
            "assignments",
            "write_document",
            {"path": path, "body": "one\n", "title": "Findings", "kind": "report"},
            ASSIGNMENT_12,
        )
        # Alex refines it in a chat: the author and the assignment stay.
        await call(app, "write_document", {"path": path, "body": "two\n", "status": "active"})
        fm = as_json(await call(app, "read_document", {"path": path}))["frontmatter"]
        items = as_json(await call(app, "list_documents", {}))
    assert res.is_error is False, res.content[0].text
    assert fm["author"] == "assignment:12"
    assert fm["assignment"] == 12
    assert fm["status"] == "active"
    assert items[0]["author"] == "assignment:12"
    assert str(items[0]["assignment"]) == "12"


async def test_an_assignment_that_edits_a_document_of_the_person_keeps_the_author(
    gateway, data_root
):
    app = gateway(both_yaml())
    path = "documents/alex/trip.md"
    async with lifespan(app):
        await call(app, "write_document", {"path": path, "body": "a\n", "title": "Trip"})
        await call_as(
            app, "assignments", "write_document", {"path": path, "body": "b\n"}, ASSIGNMENT_12
        )
        fm = as_json(await call(app, "read_document", {"path": path}))["frontmatter"]
    assert fm["author"] == "alex"
    assert fm["assignment"] == 12


async def test_only_the_assignments_caller_writes_as_an_assignment(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        # Core sends a conversation that looks like an assignment: it is not one.
        await call(
            app,
            "write_document",
            {"path": "documents/alex/a.md", "body": "x\n", "title": "A"},
            headers=ASSIGNMENT_12,
        )
        # The assignments caller with another conversation is the person.
        await call_as(
            app,
            "assignments",
            "write_document",
            {"path": "documents/alex/b.md", "body": "x\n", "title": "B"},
            {**ALEX, "X-Joshua-Conversation": "telegram:alex"},
        )
        fa = as_json(await call(app, "read_document", {"path": "documents/alex/a.md"}))
        fb = as_json(await call(app, "read_document", {"path": "documents/alex/b.md"}))
    for meta in (fa["frontmatter"], fb["frontmatter"]):
        assert meta["author"] == "alex"
        assert "assignment" not in meta


async def test_an_assignment_reaches_only_the_folders_of_its_person(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call_as(
            app,
            "assignments",
            "write_document",
            {"path": "documents/mia/x.md", "body": "x\n", "title": "X"},
            ASSIGNMENT_12,
        )
    assert res.is_error is True and "not yours" in res.content[0].text
    assert not (docs(data_root) / "mia" / "x.md").exists()


async def test_a_replace_keeps_the_fields_it_does_not_give(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app,
            "write_document",
            {
                "path": "documents/alex/p.md",
                "body": "a\n",
                "title": "Party",
                "kind": "plan",
                "tags": ["party", "june"],
                "summary": "The June party",
            },
        )
        await call(
            app,
            "write_document",
            {"path": "documents/alex/p.md", "body": "b\n", "status": "active"},
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/alex/p.md"}))[
            "frontmatter"
        ]
    assert fm["title"] == "Party"
    assert fm["kind"] == "plan"
    assert fm["tags"] == ["party", "june"]
    assert fm["summary"] == "The June party"
    assert fm["status"] == "active"


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({"kind": "recipe"}, "kind is one of"),
        ({"status": "done"}, "status is one of"),
        ({"format": "html"}, "does not match"),
        ({"tags": "a,b"}, "tags is a list"),
        ({"title": "x" * 201}, "at most 200"),
    ],
)
async def test_a_bad_field_is_refused(gateway, data_root, args, words):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_document",
            {"path": "documents/alex/x.md", "body": "x", "title": "X", **args},
        )
    assert res.is_error is True and words in res.content[0].text


async def test_an_html_document_takes_its_format_from_the_name(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_document",
            {
                "path": "documents/shared/mockup.html",
                "body": "<p>hi</p>",
                "title": "Mockup",
                "kind": "artifact",
            },
        )
        clash = await call(
            app, "write_document", {"path": "documents/shared/mockup.md", "body": "x", "title": "M"}
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/shared/mockup.html"}))
    assert res.is_error is False
    assert fm["frontmatter"]["format"] == "html"
    assert clash.is_error is True and "one slug, one document" in clash.content[0].text


async def test_a_document_over_256_kb_is_refused(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_document",
            {"path": "documents/alex/big.md", "body": "x" * (256 * 1024), "title": "Big"},
        )
    assert res.is_error is True and "256 KB" in res.content[0].text


# -- the git repository -------------------------------------------------------


@needs_git
async def test_the_root_is_a_repository_with_one_commit_per_write(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "a\n", "title": "P"}
        )
        await call(
            app,
            "write_document",
            {"path": "documents/alex/p.md", "body": "b\n", "message": "add the drinks"},
        )
        await call(app, "set_document_status", {"path": "documents/alex/p.md", "status": "final"})
    repo = docs(data_root)
    assert (repo / ".git").is_dir()
    assert (repo / "shared").is_dir()
    log = git_log(repo).splitlines()
    assert log == [
        "set_document_status: alex/p.md|Joshua",
        "write_document: alex/p.md|Joshua",
        "write_document: alex/p.md|Joshua",
    ]
    body = git(repo, "log", "-1", "--skip=1", "--format=%b")
    assert "add the drinks" in body
    # The versions of a document are its git log.
    assert len(git_log(repo, "--", "alex/p.md").splitlines()) == 3


async def test_a_failing_commit_does_not_fail_the_write(gateway, data_root, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(wikigit, "commit", boom)
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "a\n", "title": "P"}
        )
    assert res.is_error is False
    assert (docs(data_root) / "alex" / "p.md").is_file()


# -- access -------------------------------------------------------------------


async def test_a_member_never_reaches_the_folder_of_another_member(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app,
            "write_document",
            {"path": "documents/mia/secret.md", "body": "x", "title": "S"},
            headers=MIA,
        )
        await call(
            app,
            "write_document",
            {"path": "documents/shared/party.md", "body": "x", "title": "Party"},
            headers=MIA,
        )
        read = await call(app, "read_document", {"path": "documents/mia/secret.md"})
        write = await call(
            app, "write_document", {"path": "documents/mia/other.md", "body": "x", "title": "O"}
        )
        listed = await call(app, "list_documents", {"owner": "mia"})
        mine = await call(app, "list_documents", {})
        found = await call(app, "search_documents", {"query": "x"})
        comments = await call(app, "document_comments", {"path": "documents/mia/secret.md"})
        trash = await call(app, "trash_document", {"path": "documents/mia/secret.md"})
    for res in (read, write, listed, comments, trash):
        assert res.is_error is True
        assert "not yours" in res.content[0].text
    assert [item["path"] for item in as_json(mine)] == ["documents/shared/party.md"]
    assert [hit["path"] for hit in as_json(found)] == ["documents/shared/party.md"]
    assert not (docs(data_root) / "mia" / "other.md").exists()
    assert (docs(data_root) / "mia" / "secret.md").exists()


async def test_a_guest_has_no_documents_tools(gateway, data_root, config_path, monkeypatch):
    app = gateway(both_yaml())
    make_guest(config_path, monkeypatch)
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", {"X-Joshua-Person": "mia"}) as s:
            listed = await s.list_tools()
            res = await s.call_tool(
                "write_document", {"path": "documents/shared/x.md", "body": "x", "title": "X"}
            )
        guest_role = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "guest"}
        async with gateway_session(app, "/documents", "core", guest_role) as s:
            role_list = await s.list_tools()
            read = await s.call_tool("list_documents", {})
        async with gateway_session(app, "/documents", "core") as s:
            no_role = await s.list_tools()
        async with gateway_session(app, "/documents", "core", ALEX) as s:
            member = await s.list_tools()
    assert listed.tools == [] and role_list.tools == [] and no_role.tools == []
    assert res.is_error is True and "for members" in res.content[0].text
    assert read.is_error is True
    assert not docs(data_root).exists()
    assert {tool.name for tool in member.tools} == {
        "list_documents",
        "read_document",
        "write_document",
        "set_document_status",
        "search_documents",
        "promote_document",
        "trash_document",
        "document_comments",
        "resolve_document_comment",
    }


async def test_a_member_group_chat_reaches_the_shared_folder_only(gateway, data_root):
    app = gateway(both_yaml())
    group = {"X-Joshua-Person": "unknown", "X-Joshua-Role": "member"}
    async with lifespan(app):
        shared = await call(
            app,
            "write_document",
            {"path": "documents/shared/trip.md", "body": "x", "title": "Trip"},
            headers=group,
        )
        own = await call(
            app,
            "write_document",
            {"path": "documents/unknown/x.md", "body": "x", "title": "X"},
            headers=group,
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/shared/trip.md"}))
    assert shared.is_error is False
    assert own.is_error is True
    assert fm["frontmatter"]["author"] == "unknown"


TRAVERSALS = [
    "documents/../wiki/x.md",
    "documents/alex/../mia/x.md",
    "/data/documents/alex/x.md",
    "documents/alex/sub/x.md",
    "documents/alex/x.txt",
    "documents/alex/x.comments.jsonl",
    "documents/alex/x.json.md",
    "documents/alex/.hidden.md",
    "documents/.trash/x.md",
    "people/alex/x.md",
    "documents\\alex\\x.md",
    "",
]


@pytest.mark.parametrize("bad", TRAVERSALS)
async def test_a_path_never_escapes_the_documents_root(gateway, data_root, bad):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(app, "write_document", {"path": bad, "body": "x", "title": "X"})
    assert res.is_error is True
    assert str(data_root) not in res.content[0].text
    written = [p for p in data_root.rglob("*") if p.is_file()]
    assert written == []


async def test_a_symlink_out_of_the_root_is_refused(gateway, data_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (docs(data_root)).mkdir()
    (docs(data_root) / "alex").symlink_to(outside)
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app, "write_document", {"path": "documents/alex/x.md", "body": "x", "title": "X"}
        )
    assert res.is_error is True and "escapes" in res.content[0].text
    assert list(outside.iterdir()) == []


# -- list, status, search -----------------------------------------------------


async def test_list_filters_and_hides_archived(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        for slug, kind, status, tags in (
            ("a", "plan", "draft", ["party"]),
            ("b", "report", "final", []),
            ("c", "plan", "archived", ["party"]),
        ):
            await call(
                app,
                "write_document",
                {
                    "path": f"documents/alex/{slug}.md",
                    "body": f"Body {slug}.\n",
                    "title": slug.upper(),
                    "kind": kind,
                    "status": status,
                    "tags": tags,
                },
            )
        default = as_json(await call(app, "list_documents", {}))
        plans = as_json(await call(app, "list_documents", {"kind": "plan"}))
        archived = as_json(await call(app, "list_documents", {"status": "archived"}))
        everything = as_json(await call(app, "list_documents", {"include_archived": True}))
        tagged = as_json(
            await call(app, "list_documents", {"tag": "party", "include_archived": True})
        )
    assert sorted(i["path"] for i in default) == ["documents/alex/a.md", "documents/alex/b.md"]
    assert [i["path"] for i in plans] == ["documents/alex/a.md"]
    assert [i["path"] for i in archived] == ["documents/alex/c.md"]
    assert len(everything) == 3
    assert sorted(i["path"] for i in tagged) == ["documents/alex/a.md", "documents/alex/c.md"]
    item = next(i for i in default if i["path"].endswith("a.md"))
    assert item == {
        "path": "documents/alex/a.md",
        "title": "A",
        "kind": "plan",
        "status": "draft",
        "tags": ["party"],
        "summary": "Body a.",
        "author": "alex",
        "updated": STAMP,
        "assignment": None,
        "open_comments": 0,
    }


async def test_set_document_status(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "x", "title": "P"}
        )
        ok = await call(
            app, "set_document_status", {"path": "documents/alex/p.md", "status": "active"}
        )
        bad = await call(
            app, "set_document_status", {"path": "documents/alex/p.md", "status": "gone"}
        )
        missing = await call(
            app, "set_document_status", {"path": "documents/alex/q.md", "status": "final"}
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/alex/p.md"}))
    assert ok.is_error is False
    assert bad.is_error is True
    assert missing.is_error is True and "not found" in missing.content[0].text
    assert fm["frontmatter"]["status"] == "active"


async def test_search_matches_title_tags_summary_and_body(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app,
            "write_document",
            {
                "path": "documents/alex/a.md",
                "body": "line one\nThe Lasagna recipe\n",
                "title": "Dinner",
            },
        )
        await call(
            app,
            "write_document",
            {"path": "documents/shared/b.md", "body": "x", "title": "Trip", "tags": ["camping"]},
        )
        await call(
            app,
            "write_document",
            {
                "path": "documents/alex/c.md",
                "body": "lasagna",
                "title": "Old",
                "status": "archived",
            },
        )
        body_hit = as_json(await call(app, "search_documents", {"query": "lasagna"}))
        tag_hit = as_json(await call(app, "search_documents", {"query": "CAMP"}))
        with_archived = as_json(
            await call(app, "search_documents", {"query": "lasagna", "include_archived": True})
        )
    assert body_hit == [
        {
            "path": "documents/alex/a.md",
            "title": "Dinner",
            "kind": "note",
            "status": "draft",
            "snippet": "The Lasagna recipe",
        }
    ]
    assert [h["path"] for h in tag_hit] == ["documents/shared/b.md"]
    assert len(with_archived) == 2


# -- the wiki boundary ----------------------------------------------------------


@pytest.mark.parametrize(
    "front",
    ["kind: plan", "kind: report", "kind: spec", "kind: artifact", "kind: draft", "status: draft"],
)
async def test_write_file_refuses_a_document_in_the_wiki(gateway, data_root, front):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_file",
            {"path": "wiki/plans/x.md", "content": f"---\n{front}\n---\nx\n"},
            route="/files",
        )
    assert res.is_error is True
    assert boundary.WIKI_REFUSAL in res.content[0].text
    assert not (data_root / "wiki" / "plans" / "x.md").exists()


async def test_write_file_takes_a_reference_page(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_file",
            {"path": "wiki/x.md", "content": "---\nkind: wiki\nstatus: final\n---\nx\n"},
            route="/files",
        )
    assert res.is_error is False


async def test_an_append_that_makes_a_draft_is_refused(gateway, data_root):
    (data_root / "wiki" / "x.md").write_text("---\nstatus: draft\n---\n")
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "write_file",
            {"path": "wiki/x.md", "content": "more\n", "mode": "append"},
            route="/files",
        )
    assert res.is_error is True and boundary.WIKI_REFUSAL in res.content[0].text


async def test_write_document_refuses_a_wiki_path(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app, "write_document", {"path": "wiki/notes/x.md", "body": "x", "title": "X"}
        )
    assert res.is_error is True
    assert res.content[0].text == boundary.DOCUMENT_REFUSAL
    assert not (data_root / "wiki" / "notes").exists()


def test_the_boundary_is_one_function():
    boundary.check_boundary("wiki/x.md", "# x\n", tool="write_file")
    boundary.check_boundary("documents/alex/x.md", "---\nkind: plan\n---\n", tool="write_file")
    with pytest.raises(boundary.BoundaryError):
        boundary.check_boundary("wiki/x.md", "---\nKind: x\nstatus: Draft\n---\n", tool="x")
    with pytest.raises(boundary.BoundaryError):
        boundary.check_boundary("wiki/x.md", "", tool="write_document")
    assert boundary.frontmatter("---\n[1, 2]\n---\n") == {}
    assert boundary.frontmatter("---\n: :\n  - [\n---\n") == {}
    assert boundary.frontmatter("---\nno close\n") == {}


# -- promote ------------------------------------------------------------------


@needs_git
async def test_promote_writes_the_wiki_and_archives_the_document(gateway, data_root):
    wikigit.ensure_repo(data_root / "wiki")
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app,
            "write_document",
            {
                "path": "documents/alex/meals.md",
                "body": "Soup and bread.\n",
                "title": "Meals",
                "kind": "plan",
                "status": "final",
            },
        )
        res = await call(
            app,
            "promote_document",
            {
                "path": "documents/alex/meals.md",
                "wiki_path": "wiki/recipes/soup.md",
                "markdown": "# Soup\n\nBoil it.\n",
                "message": "the soup recipe",
            },
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/alex/meals.md"}))
    assert res.is_error is False, res.content[0].text
    assert as_json(res)["wiki_path"] == "wiki/recipes/soup.md"
    assert (data_root / "wiki" / "recipes" / "soup.md").read_text() == "# Soup\n\nBoil it.\n"
    assert fm["frontmatter"]["status"] == "archived"
    assert fm["frontmatter"]["promoted_to"] == "wiki/recipes/soup.md"
    # Two commits, one in each repository.
    assert git_log(data_root / "wiki").splitlines() == ["promote_document: recipes/soup.md|Joshua"]
    assert git_log(docs(data_root)).splitlines()[0] == "promote_document: alex/meals.md|Joshua"


async def test_promote_of_a_draft_page_is_refused_and_keeps_the_document(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "x", "title": "P"}
        )
        res = await call(
            app,
            "promote_document",
            {
                "path": "documents/alex/p.md",
                "wiki_path": "wiki/p.md",
                "markdown": "---\nkind: plan\n---\nx\n",
            },
        )
        journal = await call(
            app,
            "promote_document",
            {
                "path": "documents/alex/p.md",
                "wiki_path": "wiki/journal/2026/08/27/x.md",
                "markdown": "x",
            },
        )
        outside = await call(
            app,
            "promote_document",
            {"path": "documents/alex/p.md", "wiki_path": "shared/x.md", "markdown": "x"},
        )
        fm = as_json(await call(app, "read_document", {"path": "documents/alex/p.md"}))
    assert res.is_error is True and boundary.WIKI_REFUSAL in res.content[0].text
    assert journal.is_error is True and outside.is_error is True
    assert not (data_root / "wiki" / "p.md").exists()
    assert fm["frontmatter"]["status"] == "draft"
    assert "promoted_to" not in fm["frontmatter"]


# -- trash --------------------------------------------------------------------


@needs_git
async def test_trash_moves_the_document_and_its_comments(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "x\n", "title": "P"}
        )
        comments_file = docs(data_root) / "alex" / "p.comments.jsonl"
        comments_file.write_text('{"id": 1, "line_no": 1, "line_text": "x", "body": "hm"}\n')
        res = await call(app, "trash_document", {"path": "documents/alex/p.md"})
        again = await call(app, "read_document", {"path": "documents/alex/p.md"})
    assert res.is_error is False
    assert as_json(res)["comments_moved"] is True
    trash = docs(data_root) / ".trash" / "20260827T183210Z" / "alex"
    assert (trash / "p.md").is_file() and (trash / "p.comments.jsonl").is_file()
    assert not comments_file.exists()
    assert again.is_error is True
    assert git_log(docs(data_root)).splitlines()[0] == "trash_document: alex/p.md|Joshua"
    tracked = git(docs(data_root), "ls-files")
    assert "p.md" not in tracked and ".trash" not in tracked


def test_a_trash_name_in_use_gets_a_number(tmp_path):
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "p-2.md").write_text("x")
    assert docs_server._free(tmp_path / "p.md") == tmp_path / "p-3.md"
    assert docs_server._free(tmp_path / "q.md") == tmp_path / "q.md"


# -- comments -----------------------------------------------------------------


def _comment(cid, line_no, line_text, **extra):
    return {
        "id": cid,
        "line_no": line_no,
        "line_text": line_text,
        "body": f"note {cid}",
        "author": "alex",
        "created_at": STAMP,
        "resolved_at": None,
        "resolution": None,
        **extra,
    }


async def test_comments_read_stale_and_resolve(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app,
            "write_document",
            {"path": "documents/alex/p.md", "body": "# Plan\nBuy milk\nBuy eggs\n", "title": "P"},
        )
        comments_file = docs(data_root) / "alex" / "p.comments.jsonl"
        rows = [
            _comment(1, 2, "Buy milk"),
            _comment(2, 3, "Buy bread"),
            _comment(3, 9, "gone"),
            _comment(4, 1, "# Plan", resolved_at=STAMP, resolution="done"),
        ]
        comments_file.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        listed = as_json(await call(app, "document_comments", {"path": "documents/alex/p.md"}))
        everything = as_json(
            await call(
                app, "document_comments", {"path": "documents/alex/p.md", "include_resolved": True}
            )
        )
        count = as_json(await call(app, "list_documents", {}))[0]["open_comments"]
        resolved = await call(
            app,
            "resolve_document_comment",
            {"path": "documents/alex/p.md", "id": 1, "resolution": "Added  milk\n to the list"},
        )
        twice = await call(
            app,
            "resolve_document_comment",
            {"path": "documents/alex/p.md", "id": 1, "resolution": "again"},
        )
        missing = await call(
            app,
            "resolve_document_comment",
            {"path": "documents/alex/p.md", "id": 99, "resolution": "x"},
        )
        empty = await call(
            app,
            "resolve_document_comment",
            {"path": "documents/alex/p.md", "id": 2, "resolution": "  "},
        )
    assert [c["id"] for c in listed["comments"]] == [1, 2, 3]
    assert [c["stale"] for c in listed["comments"]] == [False, True, True]
    assert [c["line_now"] for c in listed["comments"]] == ["Buy milk", "Buy eggs", None]
    assert listed["open"] == 3 and listed["stale"] == 2
    assert len(everything["comments"]) == 4
    assert count == 3
    assert resolved.is_error is False and as_json(resolved)["open"] == 2
    row = json.loads(comments_file.read_text().splitlines()[0])
    assert row["resolved_at"] == STAMP
    assert row["resolution"] == "Added milk to the list"
    assert twice.is_error is True and "resolved already" in twice.content[0].text
    assert missing.is_error is True
    assert empty.is_error is True


async def test_a_comment_on_line_0_is_the_document_as_a_whole(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "x\n", "title": "Party"}
        )
        comments_file = docs(data_root) / "alex" / "p.comments.jsonl"
        rows = [_comment(1, 0, "title: Party"), _comment(2, 0, "title: Old")]
        comments_file.write_text("".join(json.dumps(r) + "\n" for r in rows))
        listed = as_json(await call(app, "document_comments", {"path": "documents/alex/p.md"}))
        # A change of the frontmatter moves no comment.
        await call(
            app,
            "write_document",
            {"path": "documents/alex/p.md", "body": "x\n", "tags": ["a", "b", "c"]},
        )
        after = as_json(await call(app, "document_comments", {"path": "documents/alex/p.md"}))
    assert [c["stale"] for c in listed["comments"]] == [False, True]
    assert listed["comments"][0]["line_now"] == "title: Party"
    assert [c["stale"] for c in after["comments"]] == [False, True]


@needs_git
async def test_a_resolve_creates_the_repository_and_commits(gateway, data_root):
    folder = docs(data_root) / "alex"
    folder.mkdir(parents=True)
    (folder / "p.md").write_text("---\ntitle: P\n---\nline\n")
    (folder / "p.comments.jsonl").write_text(json.dumps(_comment(1, 1, "line")) + "\n")
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(
            app,
            "resolve_document_comment",
            {"path": "documents/alex/p.md", "id": 1, "resolution": "ok"},
        )
    assert res.is_error is False
    assert (docs(data_root) / ".git").is_dir()
    assert git_log(docs(data_root)).splitlines() == ["resolve_document_comment: alex/p.md|Joshua"]


async def test_a_document_with_no_comments_has_none(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        await call(
            app, "write_document", {"path": "documents/alex/p.md", "body": "x", "title": "P"}
        )
        res = as_json(await call(app, "document_comments", {"path": "documents/alex/p.md"}))
    assert res == {"path": "documents/alex/p.md", "comments": [], "open": 0, "stale": 0}


# -- the gateway --------------------------------------------------------------


async def test_inventory_shows_the_documents_builtin(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/admin/inventory", headers=bearer("laptop"))
    entry = resp.json()["servers"]["documents"]
    assert entry["kind"] == "builtin"
    assert entry["status"] == "connected"
    assert len(entry["tools"]) == 9


async def test_an_unknown_tool_is_refused(gateway, data_root):
    app = gateway(both_yaml())
    async with lifespan(app):
        res = await call(app, "nope", {})
    assert res.is_error is True


# -- store units --------------------------------------------------------------


def test_join_writes_the_known_keys_first():
    meta = {"extra": 1, "title": "T", "kind": "plan", "summary": None}
    assert store.join(meta, "body\n") == "---\ntitle: T\nkind: plan\nextra: 1\n---\nbody\n"
    assert store.plain({"a": [datetime(2026, 1, 2, tzinfo=UTC)]}) == {
        "a": ["2026-01-02T00:00:00+00:00"]
    }
    assert store.has_frontmatter("---\nx: 1\n---\nb") is True
    assert store.has_frontmatter("---\nno close") is False


def test_the_field_checks():
    assert store.one_line(None, "x", 5) is None
    assert store.one_line("  ", "x", 5) is None
    with pytest.raises(store.DocumentError):
        store.one_line(3, "x", 5)
    with pytest.raises(store.DocumentError):
        store.one_line("", "x", 5, required=True)
    assert store.tags_of(["a", " a ", "", "b"]) == ["a", "b"]
    with pytest.raises(store.DocumentError):
        store.tags_of([str(i) for i in range(21)])
    with pytest.raises(store.DocumentError):
        store.tags_of(["x" * 41])


def test_a_resolution_keeps_every_other_line(tmp_path):
    path = tmp_path / "p.comments.jsonl"
    path.write_text('{"id": 1, "body": "a"}\nnot json\n{"id": 2, "body": "b"}\n')
    store.set_resolution(path, 2, "done", STAMP)
    lines = path.read_text().split("\n")
    assert lines[0] == '{"id": 1, "body": "a"}' and lines[1] == "not json"
    assert json.loads(lines[2])["resolution"] == "done"

"""Offline tests for the JSON routes of the documents.

``GET /api/v1/documents`` lists the documents of the member who signed in and
the shared ones, one status at a time.
``GET /api/v1/documents/{owner}/{slug}`` gives one document as data: its
frontmatter, its blocks, every line of its body, its comments and its
versions. The two comment routes write one comment and remove one.

A member reads their own folder and ``shared/``; another member's document is
404, never 403, and a guest gets 404 on every route.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_docstore as ds
from joshua_gateway.viewer_api import documents as api
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")
SAM = ("sam", "sam-secret")
GUEST = ("gus", "gus-secret")
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: UTC
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
      - id: gus
        name: Gus
        role: guest
      - id: sam
        name: Sam
        role: owner
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
        gus: ${VIEWER_PW_GUS:-}
        sam: ${VIEWER_PW_SAM:-}
    """)

PLAN = textwrap.dedent("""\
    ---
    title: Party plan
    kind: plan
    status: active
    summary: The plan for the party on Saturday.
    updated: 2026-11-09T12:00:00+00:00
    ---
    # Party plan

    Food and music for twenty people.

    - Order the food
    - Book the music
    """)

REPORT = textwrap.dedent("""\
    ---
    title: Docs cleanup
    kind: report
    status: final
    updated: 2026-11-08T12:00:00+00:00
    ---
    # Docs cleanup
    The docs read as one set now.

    ## Highlights
    - **12 pages** fixed

    ## What was done
    I read every page.
    """)

HTML_DOC = textwrap.dedent("""\
    ---
    title: Mockup
    kind: artifact
    status: active
    ---
    <style>h1 { color: red }</style><h1>Hi</h1>
    """)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    monkeypatch.setenv("VIEWER_PW_GUS", GUEST[1])
    monkeypatch.setenv("VIEWER_PW_SAM", SAM[1])
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    monkeypatch.setattr(viewer, "_now", lambda tz: NOW.astimezone(tz))
    return root


@pytest.fixture
def docs(data) -> Path:
    """A documents folder with a plan and a report of alex, a shared note, and
    a plan of mia, in a git repository with one commit."""
    base = data / "documents"
    _write(base / "alex/party.md", PLAN)
    _write(base / "alex/cleanup.md", REPORT)
    _write(
        base / "shared/meals.md",
        "---\ntitle: Meal plan\nkind: note\nstatus: active\n---\nMonday: soup.\n",
    )
    _write(base / "mia/secret.md", "---\ntitle: Mia private\nstatus: active\n---\nPrivate.\n")
    if shutil.which("git") is not None:
        wikigit.ensure_repo(base)
        wikigit.commit(base, None, "write_document: documents")
    return base


def client() -> TestClient:
    return TestClient(viewer.build_app())


def _git_log(base: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-c", f"safe.directory={base}", "log", "--format=%an|%s"],
        cwd=base,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def _list(auth=ALEX, **params) -> Any:
    return client().get("/api/v1/documents", params=params, auth=auth)


def _doc(owner: str, slug: str, auth=ALEX, **params) -> Any:
    return client().get(f"/api/v1/documents/{owner}/{slug}", params=params, auth=auth)


def _token(owner: str, slug: str, person: str = "alex") -> str:
    return viewer._csrf_token(person, api.comments_scope(f"/documents/{owner}/{slug}"))


def _add(owner: str, slug: str, *, auth=ALEX, c: TestClient | None = None, **fields) -> Any:
    body = {"csrf": _token(owner, slug, auth[0]), **fields}
    return (c or client()).post(f"/api/v1/documents/{owner}/{slug}/comments", json=body, auth=auth)


def _remove(owner: str, slug: str, cid: str, *, auth=ALEX, c: TestClient | None = None) -> Any:
    return (c or client()).post(
        f"/api/v1/documents/{owner}/{slug}/comments/{cid}/delete",
        json={"csrf": _token(owner, slug, auth[0])},
        auth=auth,
    )


# -- the list ---------------------------------------------------------------


def test_the_list_gives_the_active_own_and_shared_documents(docs) -> None:
    response = _list()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "active" and body["kind"] is None
    assert body["counts"] == {"active": 2, "draft": 0, "final": 1, "archived": 0}
    assert body["kinds"] == list(ds.KINDS)
    titles = [row["title"] for row in body["documents"]]
    assert sorted(titles) == ["Meal plan", "Party plan"]
    assert "Mia private" not in titles and "Docs cleanup" not in titles
    party = next(row for row in body["documents"] if row["title"] == "Party plan")
    assert party == {
        "path": "documents/alex/party.md",
        "url": "/documents/alex/party",
        "title": "Party plan",
        "kind": "plan",
        "status": "active",
        "owner": "alex",
        "owner_label": "yours",
        "summary": "The plan for the party on Saturday.",
        "tags": [],
        "updated": "2026-11-09T12:00:00+00:00",
        "open_comments": 0,
    }
    meals = next(row for row in body["documents"] if row["title"] == "Meal plan")
    assert meals["owner_label"] == "shared"


def test_the_status_and_the_kind_of_the_query(docs) -> None:
    final = _list(status="final").json()
    assert [row["title"] for row in final["documents"]] == ["Docs cleanup"]
    plans = _list(kind="plan").json()
    assert [row["title"] for row in plans["documents"]] == ["Party plan"]
    assert plans["kind"] == "plan"
    # The counts follow the kind, because a tab keeps the chip of the kind.
    assert plans["counts"] == {"active": 1, "draft": 0, "final": 0, "archived": 0}
    assert _list(status="archived").json()["documents"] == []
    # An unknown status is the default, and a bad kind is no kind.
    assert _list(status="bogus").json()["status"] == "active"
    assert _list(kind="<x>").json()["kind"] is None


def test_the_tag_of_the_query_keeps_the_documents_with_that_tag(docs) -> None:
    _write(
        docs / "alex/pie.md",
        "---\ntitle: Pie\nkind: note\nstatus: active\ntags: [ws:thanksgiving, food]\n---\nPie.\n",
    )
    body = _list(tag="ws:thanksgiving").json()
    assert [row["title"] for row in body["documents"]] == ["Pie"]
    assert body["documents"][0]["tags"] == ["ws:thanksgiving", "food"]
    assert body["tag"] == "ws:thanksgiving"
    assert body["counts"]["active"] == 1
    assert _list(tag="ws:nothing").json()["documents"] == []
    assert _list().json()["tag"] is None


def test_a_row_counts_only_the_open_comments(docs) -> None:
    lines = [
        {"id": 1, "line_no": 1, "line_text": "# Party plan", "body": "a", "author": "alex"},
        {
            "id": 2,
            "line_no": 1,
            "line_text": "# Party plan",
            "body": "b",
            "author": "alex",
            "resolved_at": "2026-11-09T13:00:00+00:00",
            "resolution": "done",
        },
    ]
    _write(docs / "alex/party.comments.jsonl", "".join(json.dumps(x) + "\n" for x in lines))
    rows = _list().json()["documents"]
    assert next(row for row in rows if row["title"] == "Party plan")["open_comments"] == 1


def test_with_no_documents_folder_the_list_is_empty_and_creates_nothing(data) -> None:
    body = _list().json()
    assert body["documents"] == []
    assert body["counts"] == {"active": 0, "draft": 0, "final": 0, "archived": 0}
    assert not (data / "documents").exists()
    assert _doc("alex", "party").status_code == 404
    assert not (data / "documents").exists()


# -- one document -----------------------------------------------------------


def test_a_markdown_document_gives_its_blocks_lines_and_token(docs) -> None:
    response = _doc("alex", "party")
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "/documents/alex/party"
    assert body["path"] == "documents/alex/party.md"
    assert (body["title"], body["kind"], body["status"]) == ("Party plan", "plan", "active")
    assert body["format"] == "markdown" and body["artifact_url"] is None
    assert body["owner"] == "alex" and body["owner_label"] == "yours"
    assert body["frontmatter"]["summary"] == "The plan for the party on Saturday."
    assert body["frontmatter"]["updated"] == "2026-11-09T12:00:00+00:00"
    # Every line of the body, which a comment counts from 1, and the
    # frontmatter, which the Source fold shows with no "+".
    assert body["lines"][0] == "# Party plan"
    assert body["lines"][4] == "- Order the food"
    assert len(body["lines"]) == 7
    assert body["preamble"][0] == "---" and body["preamble"][1] == "title: Party plan"
    assert len(body["preamble"]) == 7
    assert body["comments"] == [] and body["report"]["structured"] is False
    assert body["version"] is None
    assert body["csrf"] == _token("alex", "party")


def test_each_block_keeps_the_lines_of_the_body(docs) -> None:
    blocks = _doc("alex", "party").json()["blocks"]
    assert [b["kind"] for b in blocks] == ["h", "p", "li", "li"]
    assert [b["first_line"] for b in blocks] == [1, 3, 5, 6]
    assert [b["id"] for b in blocks] == ["L1", "L3", "L5", "L6"]
    assert blocks[0]["markdown"] == "# Party plan"
    assert blocks[2]["markdown"] == "- Order the food"
    assert blocks[2]["last_line"] == 5 and blocks[2]["depth"] == 0


def test_a_nested_list_item_keeps_its_marker_and_its_depth(docs) -> None:
    _write(
        docs / "alex/deep.md",
        "---\ntitle: Deep\n---\n- One\n    - Two\n",
    )
    blocks = _doc("alex", "deep").json()["blocks"]
    assert [(b["kind"], b["depth"], b["markdown"]) for b in blocks] == [
        ("li", 0, "- One"),
        ("li", 1, "- Two"),
    ]


def test_the_sections_of_a_report_shaped_document_are_data(docs) -> None:
    body = _doc("alex", "cleanup").json()
    report = body["report"]
    assert report is not None and report["structured"] is True
    assert report["title"] == "Docs cleanup"
    assert report["lede"] == "The docs read as one set now."
    assert set(report["sections"]) == {"highlights", "what was done"}
    first = report["sections"]["highlights"]
    assert first["key"] == "highlights"
    assert "12 pages" in first["body"] and first["line"] > 0
    # A card of the showcase sits on its own line of the body, so a comment
    # anchors to it.
    [one] = first["items"]
    assert one["number"] == 1 and one["lead"] == "12 pages"
    assert one["first_line"] == first["start"] == first["line"] + 1
    assert [block["id"] for block in first["blocks"]] == [f"L{first['start']}"]
    # The page of this task renders the blocks, so they are there too.
    assert body["blocks"][0]["kind"] == "h"


def test_a_document_with_no_report_headings_is_not_structured(docs) -> None:
    report = _doc("alex", "party").json()["report"]
    assert report["structured"] is False
    assert report["sections"] == {} and report["extra"] == []
    # With no section there is nothing to show as a showcase: the page falls
    # back to the blocks of the body.
    assert report["preamble"] is None and report["title"] == "Party plan"


def test_a_decision_of_a_document_carries_the_text_to_copy(docs) -> None:
    _write(
        docs / "alex/choose.md",
        "---\ntitle: Choose\nkind: report\nstatus: final\n---\n"
        "# Choose\nOne thing is left.\n\n## Outputs\n"
        "- document: documents/alex/party.md — Party plan\n"
        "- merge request: https://git.example.test/mr/4 — The fix\n\n"
        "## Decisions for you\n- **Pick a date** Saturday or Sunday?\n",
    )
    report = _doc("alex", "choose").json()["report"]
    [decision] = report["sections"]["decisions for you"]["items"]
    assert decision["question"] == "Pick a date" and decision["context"] == "Saturday or Sunday?"
    assert decision["copy"] == "Document documents/alex/choose.md, decision 1: Pick a date"
    outputs = report["sections"]["outputs"]["outputs"]
    assert [out["icon"] for out in outputs] == ["page", "merge request"]
    assert outputs[0]["url"] == "/documents/alex/party"
    assert outputs[1]["url"] == "https://git.example.test/mr/4"


def test_an_html_document_names_its_artifact_and_has_no_block(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    body = _doc("alex", "mockup").json()
    assert body["format"] == "html"
    assert body["artifact_url"] == "/documents/alex/mockup/artifact"
    assert body["blocks"] == [] and body["report"] is None
    assert body["frontmatter"].get("scripts") is None
    # The body is in the frame, and its lines are in the Source fold.
    assert body["lines"][0].startswith("<style>")


def test_the_frontmatter_says_whether_scripts_run(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC.replace("status: active", "scripts: true"))
    assert _doc("alex", "mockup").json()["frontmatter"]["scripts"] is True


@NEEDS_GIT
def test_an_older_version_is_read_only(docs) -> None:
    _write(docs / "alex/party.md", PLAN.replace("Book the music", "Book the band"))
    wikigit.commit(docs, [docs / "alex/party.md"], "write_document: documents/alex/party.md")
    body = _doc("alex", "party").json()
    assert len(body["versions"]) == 2
    assert body["versions"][0]["message"] == "write_document: documents/alex/party.md"
    old = body["versions"][1]["sha"]
    older = _doc("alex", "party", rev=old).json()
    assert older["lines"][5] == "- Book the music"
    assert older["version"] == {"sha": old, "when": body["versions"][1]["when"]}
    assert older["comments"] == [] and older["csrf"] is None
    assert older["versions"] == body["versions"]
    # A prefix of a version works; any other value is 404.
    assert _doc("alex", "party", rev=old[:10]).status_code == 200
    assert _doc("alex", "party", rev="HEAD").status_code == 404
    assert _doc("alex", "party", rev="0" * 40).status_code == 404


@NEEDS_GIT
def test_an_older_version_of_an_html_document_frames_that_version(docs) -> None:
    path = docs / "alex/mockup.html"
    _write(path, HTML_DOC)
    wikigit.commit(docs, [path], "write_document: mockup")
    old = _doc("alex", "mockup").json()["versions"][0]["sha"]
    body = _doc("alex", "mockup", rev=old).json()
    assert body["artifact_url"] == f"/documents/alex/mockup/artifact?rev={old}"


def test_no_repository_gives_no_version(data) -> None:
    _write(data / "documents/alex/note.md", "---\ntitle: Note\n---\nHello\n")
    assert _doc("alex", "note").json()["versions"] == []
    assert _doc("alex", "note", rev="abcdef1").status_code == 404


def test_the_comments_of_a_document_say_where_they_are(docs) -> None:
    lines = [
        {"id": 1, "line_no": 0, "line_text": "title: Party plan", "body": "rename", "author": "a"},
        {"id": 2, "line_no": 5, "line_text": "- Order the cake", "body": "x", "author": "alex"},
        {
            "id": 3,
            "line_no": 1,
            "line_text": "# Party plan",
            "body": "y",
            "author": "alex",
            "resolved_at": "2026-11-09T13:00:00+00:00",
            "resolution": "done",
        },
    ]
    _write(docs / "alex/party.comments.jsonl", "".join(json.dumps(x) + "\n" for x in lines))
    comments = _doc("alex", "party").json()["comments"]
    assert [c["id"] for c in comments] == ["1", "2", "3"]
    whole, moved, done = comments
    assert whole["line_no"] == 0 and whole["line_now"] == "title: Party plan"
    assert whole["stale"] is False and whole["author"] == "a"
    # The stored line is no longer that line of the body.
    assert moved["stale"] is True and moved["line_now"] == "- Order the food"
    assert done["resolved_at"] == "2026-11-09T13:00:00+00:00"
    assert done["resolution"] == "done"


# -- the owner rule ---------------------------------------------------------


def test_another_members_document_is_404_never_403(docs) -> None:
    assert _doc("mia", "secret").status_code == 404
    assert _doc("mia", "secret").json() == {"error": "not found"}
    assert _doc("mia", "secret", auth=MIA).status_code == 200
    assert _doc("shared", "meals", auth=MIA).status_code == 200
    assert _doc("alex", "party", auth=MIA).status_code == 404
    assert "Mia private" not in _list().text


@pytest.mark.parametrize(
    ("owner", "slug"),
    [
        ("alex", "%2e%2e"),
        ("alex", ".hidden"),
        ("alex", "party.md"),
        ("Alex", "party"),
        ("wiki", "index"),
        ("alex", "nothing"),
    ],
)
def test_a_bad_address_is_404(docs, owner, slug) -> None:
    assert _doc(owner, slug).status_code == 404


def test_a_guest_gets_404_on_every_documents_route(docs) -> None:
    assert _list(auth=GUEST).status_code == 404
    assert _list(auth=GUEST).json() == {"error": "not found"}
    assert _doc("shared", "meals", auth=GUEST).status_code == 404
    assert _add("shared", "meals", auth=GUEST, line_no=1, body="hi").status_code == 404
    assert _remove("shared", "meals", "1", auth=GUEST).status_code == 404
    assert not (docs / "shared/meals.comments.jsonl").exists()


def test_an_owner_reads_the_documents_as_a_member_does(docs) -> None:
    """The member gate of the documents routes admits the owner role."""
    body = _list(auth=SAM).json()
    assert [row["title"] for row in body["documents"]] == ["Meal plan"]
    response = _doc("shared", "meals", auth=SAM)
    assert response.status_code == 200
    assert response.json()["title"] == "Meal plan"


def test_every_route_needs_a_sign_in(docs) -> None:
    c = client()
    assert c.get("/api/v1/documents").status_code == 401
    assert c.get("/api/v1/documents/alex/party").status_code == 401
    assert c.post("/api/v1/documents/alex/party/comments", json={}).status_code == 401
    assert c.post("/api/v1/documents/alex/party/comments/1/delete", json={}).status_code == 401


# -- a comment --------------------------------------------------------------


@NEEDS_GIT
def test_a_comment_is_added_and_the_answer_carries_the_comments(docs) -> None:
    response = _add("alex", "party", line_no=5, line_text="- Order the food", body="Pizza")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["anchor"] == "L5"
    assert [c["body"] for c in body["comments"]] == ["Pizza"]
    stored = json.loads((docs / "alex/party.comments.jsonl").read_text())
    assert stored["author"] == "alex" and stored["line_no"] == 5
    assert stored["line_text"] == "- Order the food" and stored["body"] == "Pizza"
    assert stored["created_at"] == NOW.isoformat() and stored["resolved_at"] is None
    assert _git_log(docs)[0] == "Joshua|viewer: comment documents/alex/party.md"
    # The author deletes it: the file is empty again.
    cid = body["comments"][0]["id"]
    removed = _remove("alex", "party", cid)
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "comments": []}
    assert (docs / "alex/party.comments.jsonl").read_text().strip() == ""
    # Each write of a comment is one commit of the documents repository.
    assert _git_log(docs)[:2] == ["Joshua|viewer: comment documents/alex/party.md"] * 2


def test_a_comment_on_line_0_takes_the_title_line(docs) -> None:
    response = _add("alex", "party", line_no=0, body="Shorter title")
    assert response.status_code == 200
    assert response.json()["anchor"] == "L0"
    stored = json.loads((docs / "alex/party.comments.jsonl").read_text())
    assert stored["line_no"] == 0 and stored["line_text"] == "title: Party plan"


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"line_no": -1, "body": "x"}, "The line is not in the document."),
        ({"line_no": 999, "body": "x"}, "The line is not in the document."),
        ({"line_no": "nope", "body": "x"}, "The line is not in the document."),
        ({"line_no": True, "body": "x"}, "The line is not in the document."),
        ({"body": "x"}, "The line is not in the document."),
        ({"line_no": 1, "body": "  "}, "The comment is empty."),
        ({"line_no": 1}, "The comment is empty."),
        ({"line_no": 1, "body": "x" * 2001}, "The comment exceeds 2000 characters."),
    ],
)
def test_a_bad_comment_is_refused(docs, fields, error) -> None:
    response = _add("alex", "party", **fields)
    assert response.status_code == 400
    assert response.json() == {"error": error}
    assert not (docs / "alex/party.comments.jsonl").exists()


def test_a_comment_needs_the_token_of_its_own_page(docs) -> None:
    c = client()
    fields = {"line_no": 5, "body": "x"}
    assert (
        c.post("/api/v1/documents/alex/party/comments", json=fields, auth=ALEX).status_code == 403
    )
    for token in ("stale", _token("shared", "meals"), _token("alex", "party", "mia")):
        response = c.post(
            "/api/v1/documents/alex/party/comments", json={"csrf": token, **fields}, auth=ALEX
        )
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
    assert not (docs / "alex/party.comments.jsonl").exists()


def test_a_cross_site_origin_is_403(docs) -> None:
    evil = {"Origin": "https://evil.example.com"}
    response = client().post(
        "/api/v1/documents/alex/party/comments",
        json={"csrf": _token("alex", "party"), "line_no": 5, "body": "x"},
        auth=ALEX,
        headers=evil,
    )
    assert response.status_code == 403
    _write(docs / "alex/party.comments.jsonl", json.dumps({"id": 1, "line_no": 1}) + "\n")
    removed = client().post(
        "/api/v1/documents/alex/party/comments/1/delete",
        json={"csrf": _token("alex", "party")},
        auth=ALEX,
        headers=evil,
    )
    assert removed.status_code == 403


def test_a_body_that_is_not_a_json_object_is_refused(docs) -> None:
    response = client().post(
        "/api/v1/documents/alex/party/comments",
        content=b"[]",
        headers={"Content-Type": "application/json"},
        auth=ALEX,
    )
    assert response.status_code == 400


def test_a_comment_on_another_members_document_is_404(docs) -> None:
    assert _add("mia", "secret", line_no=1, body="x").status_code == 404
    assert not (docs / "mia/secret.comments.jsonl").exists()


def test_only_the_author_deletes_an_open_comment(docs) -> None:
    mine = {"id": 1, "line_no": 1, "line_text": "", "body": "of mia", "author": "mia"}
    done = {"id": 2, "line_no": 1, "body": "old", "author": "alex", "resolved_at": "x"}
    text = json.dumps(mine) + "\n" + json.dumps(done) + "\n"
    _write(docs / "shared/meals.comments.jsonl", text)
    for cid in ("1", "2", "99"):
        response = _remove("shared", "meals", cid)
        assert response.status_code == 404
        assert response.json() == {"error": "not found"}
    assert (docs / "shared/meals.comments.jsonl").read_text() == text


def test_a_comment_with_no_repository_is_stored_with_no_commit(data) -> None:
    _write(data / "documents/alex/note.md", "---\ntitle: Note\n---\nHello\n")
    response = _add("alex", "note", line_no=1, body="x")
    assert response.status_code == 200
    stored = json.loads((data / "documents/alex/note.comments.jsonl").read_text())
    assert stored["line_text"] == "Hello"
    assert not (data / "documents/.git").exists()

"""The documents section of the viewer: the list, a page, comments, versions,
and HTML documents in a sandboxed frame."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_comments as vc
from joshua_gateway import viewer_docstore as ds
from joshua_gateway import viewer_documents as dm
from joshua_gateway import viewer_report as rp
from joshua_gateway import viewer_showcase as sc
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")
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
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
        gus: ${VIEWER_PW_GUS:-}
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
    - **3 links** repaired

    ## What was asked
    Clean up the docs.

    ## What was done
    I read every page.

    ## Outputs
    - page: [Ops](https://wiki.example.com/ops) — the main page

    ## Decisions for you
    1. **Delete the old page?** It has no readers.

    ## Follow-ups
    - Check the links again
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


def _get(path: str, auth=ALEX, **params):
    return client().get(path, params=params, auth=auth)


def _main(html: str) -> str:
    return html[html.index("<main") : html.index("</main>")]


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _git_log(base: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-c", f"safe.directory={base}", "log", "--format=%an|%s"],
        cwd=base,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


# -- the nav and access -----------------------------------------------------


def test_the_nav_has_documents_for_a_member(data) -> None:
    html = _get("/wiki/").text
    assert 'href="/documents">' in html and ">Documents</a>" in html


def test_a_guest_has_no_documents_section(docs) -> None:
    html = _get("/wiki/", auth=GUEST).text
    assert 'href="/documents"' not in html
    assert _get("/documents", auth=GUEST).status_code == 404
    assert _get("/documents/shared/meals", auth=GUEST).status_code == 404
    response = client().post(
        "/documents/shared/meals/comments",
        data={"csrf": "x", "line_no": "1", "body": "hi"},
        auth=GUEST,
    )
    assert response.status_code == 404


def test_each_page_needs_a_sign_in(docs) -> None:
    assert client().get("/documents").status_code == 401
    assert client().get("/documents/alex/party").status_code == 401


def test_with_no_documents_folder_the_list_is_empty_and_creates_nothing(data) -> None:
    response = _get("/documents")
    assert response.status_code == 200
    assert "No documents yet." in response.text
    assert not (data / "documents").exists()
    assert _get("/documents/alex/party").status_code == 404
    assert not (data / "documents").exists()


def test_a_member_never_reads_another_members_document(docs) -> None:
    listing = _get("/documents").text
    assert "Mia private" not in listing
    assert _get("/documents/mia/secret").status_code == 404
    # Mia reads her own, and the shared one, and not Alex's.
    assert _get("/documents/mia/secret", auth=MIA).status_code == 200
    assert _get("/documents/shared/meals", auth=MIA).status_code == 200
    assert _get("/documents/alex/party", auth=MIA).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/documents/alex/%2e%2e",
        "/documents/..%2fmia/secret",
        "/documents/alex/.hidden",
        "/documents/alex/party.md",
        "/documents/alex/party.comments",
        "/documents/Alex/party",
        "/documents/wiki/index",
    ],
)
def test_a_bad_path_is_404(docs, path) -> None:
    assert _get(path).status_code == 404


def test_a_link_out_of_the_folder_is_404(docs, tmp_path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: Outside\n---\nNo.\n")
    (docs / "alex/link.md").symlink_to(outside)
    assert _get("/documents/alex/link").status_code == 404
    assert "Outside" not in _get("/documents", status="draft").text


# -- the list ---------------------------------------------------------------


def test_the_list_shows_active_own_and_shared_by_default(docs) -> None:
    html = _main(_get("/documents").text)
    assert '<span class="j-chip on" aria-current="true">Active</span>' in html
    for label in ("Drafts", "Final", "Archived"):
        assert f">{label}</a>" in html
    assert "Party plan" in html and "Meal plan" in html
    assert "Docs cleanup" not in html  # final
    assert "<table" not in html
    assert "The plan for the party on Saturday." in html
    assert "yours" in html and "shared" in html
    # The newest first.
    assert html.index("Party plan") < html.index("Meal plan") or "updated" in html


def test_the_status_and_kind_filters(docs) -> None:
    final = _main(_get("/documents", status="final").text)
    assert "Docs cleanup" in final and "Party plan" not in final
    plans = _main(_get("/documents", kind="plan").text)
    assert "Party plan" in plans and "Meal plan" not in plans
    assert '<span class="j-chip sm on" aria-current="true">plan</span>' in plans
    none = _main(_get("/documents", status="archived").text)
    assert "No archived document." in none
    assert "of the kind note" in _main(_get("/documents", status="final", kind="note").text)
    # An unknown status gives the default.
    assert "Party plan" in _main(_get("/documents", status="bogus").text)


def test_a_row_counts_the_open_comments(docs) -> None:
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
    html = _main(_get("/documents").text)
    assert "1 comment for Joshua" in html


def test_a_hostile_title_and_summary_are_escaped(docs) -> None:
    _write(
        docs / "alex/bad.md",
        '---\ntitle: "<script>alert(1)</script>"\nstatus: active\n'
        'summary: "<img src=x onerror=alert(1)>"\n---\nBody\n',
    )
    html = _get("/documents").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    page = _get("/documents/alex/bad").text
    assert "<script>alert(1)" not in page


# -- a Markdown document ----------------------------------------------------


def test_a_markdown_document_renders_as_blocks_with_a_plus_each(docs) -> None:
    html = _main(_get("/documents/alex/party").text)
    # The body has its own title, so the head gives none.
    assert html.count("<h1") == 1 and "<h1>Party plan</h1>" in html
    # Each block is a unit anchored to its line of the body, from 1.
    assert 'id="L1"' in html and 'id="L3"' in html and 'id="L5"' in html
    assert '<input type="hidden" name="line_no" value="5">' in html
    assert '<input type="hidden" name="line_text" value="- Order the food">' in html
    assert "Comment on the list item at line 6" in html
    source = html[html.index('<details class="a-source">') :]
    # The Source fold shows the frontmatter with no number and no "+", then
    # numbers the body from 1.
    assert "title: Party plan" in source
    pre = source[: source.index('id="S1"')]
    assert pre.count('class="a-line a-pre"') == 7 and "a-add" not in pre
    assert '<li class="a-line" id="S1"><a class="a-ln" href="#S1">1</a>' in source
    assert '<code class="a-code"># Party plan</code>' in source
    assert '<span class="j-badge accent">active</span>' in html


def test_a_report_shaped_document_shows_the_showcase(docs) -> None:
    html = _main(_get("/documents/alex/cleanup").text)
    assert '<ul class="a-cards a-hls">' in html
    assert '<ol class="a-decisions">' in html
    assert '<p class="a-lede">The docs read as one set now.</p>' in html
    assert "Tell Joshua your answer." in html
    # The cards have their own "+", at their line of the file.
    assert "Comment on the highlight" in html and "Comment on decision 1" in html
    assert 'id="L5"' in html  # the first highlight
    # The copy script is the one the policy allows.
    assert f"<script>{sc.COPY_SCRIPT}</script>" in html
    assert "Document documents/alex/cleanup.md, decision 1: Delete the old page?" in html


def test_both_sections_use_one_renderer() -> None:
    """The document page and the assignment page render a report with the
    same functions: with no gutter, a section is the HTML of an assignment."""
    report = rp.parse(REPORT.split("---\n", 2)[2])
    plain = sc.highlights_html(report.get(rp.HIGHLIGHTS))
    assert '<li class="a-hl"><strong class="a-hl-lead">12 pages</strong>' in plain
    from joshua_gateway import viewer_assignments as am

    assert am.sc is sc
    assert dm.sc is sc
    assert am.Gutter is vc.Gutter and dm.Gutter is vc.Gutter


def test_a_comment_on_the_document_as_a_whole_shows_under_the_head(docs) -> None:
    line = {"id": 1, "line_no": 0, "line_text": "title: Party plan", "body": "rename"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line | {"author": "alex"}) + "\n")
    html = _main(_get("/documents/alex/party").text)
    head = html[html.index("<header") : html.index("</header>")]
    assert "rename" in head and 'id="L0"' in head
    assert "line changed" not in head
    # The "+" of the head is line 0, anchored to the title line.
    assert '<input type="hidden" name="line_no" value="0">' in head
    assert '<input type="hidden" name="line_text" value="title: Party plan">' in head
    assert "Comment on the document" in head


def test_a_comment_on_the_document_is_stale_when_the_title_changes(docs) -> None:
    line = {"id": 1, "line_no": 0, "line_text": "title: Old plan", "body": "x", "author": "a"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line) + "\n")
    doc = ds.find(docs, "alex", "party", ["alex"])
    assert doc is not None and [c.stale for c in ds.read_comments(doc)] == [True]
    head = _main(_get("/documents/alex/party").text)
    assert "line changed" in head[head.index("<header") : head.index("</header>")]


def test_a_frontmatter_change_never_moves_a_comment(docs) -> None:
    line = {"id": 1, "line_no": 5, "line_text": "- Order the food", "body": "x", "author": "a"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line) + "\n")
    _write(docs / "alex/party.md", PLAN.replace("kind: plan\n", "kind: plan\ntags:\n- a\n- b\n"))
    doc = ds.find(docs, "alex", "party", ["alex"])
    assert doc is not None and [c.stale for c in ds.read_comments(doc)] == [False]


def test_a_stale_comment_is_marked(docs) -> None:
    line = {"id": 1, "line_no": 5, "line_text": "- Order the cake", "body": "x", "author": "a"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line) + "\n")
    html = _main(_get("/documents/alex/party").text)
    assert "line changed" in html and "- Order the cake" in html
    # Another person's comment has no Delete for alex.
    assert "/comments/1/delete" not in html


def test_a_bad_comments_file_line_is_skipped(docs) -> None:
    _write(docs / "alex/party.comments.jsonl", "not json\n[1]\n{}\n")
    assert _get("/documents/alex/party").status_code == 200


# -- comments ---------------------------------------------------------------


@NEEDS_GIT
def test_the_comment_round_trip(docs) -> None:
    c = client()
    page = c.get("/documents/alex/party", auth=ALEX).text
    token = _csrf(page)
    response = c.post(
        "/documents/alex/party/comments",
        data={"csrf": token, "line_no": "5", "line_text": "- Order the food", "body": "Pizza"},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/documents/alex/party#L5"
    stored = (docs / "alex/party.comments.jsonl").read_text().splitlines()
    record = json.loads(stored[0])
    assert set(record) == {
        "id",
        "line_no",
        "line_text",
        "body",
        "author",
        "created_at",
        "resolved_at",
        "resolution",
    }
    assert record["author"] == "alex" and record["line_no"] == 5 and record["body"] == "Pizza"
    assert record["created_at"] == NOW.isoformat() and record["resolved_at"] is None
    assert _git_log(docs)[0] == "Joshua|viewer: comment documents/alex/party.md"
    html = c.get("/documents/alex/party", auth=ALEX).text
    assert '<p class="a-comment-body">Pizza</p>' in html
    assert "1 comment for Joshua" in html
    # The author deletes it: the line goes, and one more commit.
    response = c.post(
        f"/documents/alex/party/comments/{record['id']}/delete",
        data={"csrf": token},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (docs / "alex/party.comments.jsonl").read_text().strip() == ""
    assert _git_log(docs)[:2] == ["Joshua|viewer: comment documents/alex/party.md"] * 2


@NEEDS_GIT
def test_only_the_author_deletes_an_open_comment(docs) -> None:
    mine = {"id": 1, "line_no": 1, "line_text": "", "body": "of mia", "author": "mia"}
    done = {"id": 2, "line_no": 1, "body": "old", "author": "alex", "resolved_at": "x"}
    text = json.dumps(mine) + "\n" + json.dumps(done) + "\n"
    _write(docs / "shared/meals.comments.jsonl", text)
    c = client()
    token = _csrf(c.get("/documents/shared/meals", auth=ALEX).text)
    for cid in ("1", "2", "99"):
        response = c.post(
            f"/documents/shared/meals/comments/{cid}/delete", data={"csrf": token}, auth=ALEX
        )
        assert response.status_code == 404
    assert (docs / "shared/meals.comments.jsonl").read_text() == text


def test_a_comment_needs_the_token_and_the_same_origin(docs) -> None:
    c = client()
    token = _csrf(c.get("/documents/alex/party", auth=ALEX).text)
    form = {"line_no": "5", "body": "x"}
    assert c.post("/documents/alex/party/comments", data=form, auth=ALEX).status_code == 403
    # The token of another document does not pass.
    other = _csrf(c.get("/documents/shared/meals", auth=ALEX).text)
    response = c.post("/documents/alex/party/comments", data=form | {"csrf": other}, auth=ALEX)
    assert response.status_code == 403
    response = c.post(
        "/documents/alex/party/comments",
        data=form | {"csrf": token},
        auth=ALEX,
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert not (docs / "alex/party.comments.jsonl").exists()


def test_a_comment_on_another_members_document_is_404(docs) -> None:
    c = client()
    token = _csrf(c.get("/documents/alex/party", auth=ALEX).text)
    response = c.post(
        "/documents/mia/secret/comments",
        data={"csrf": token, "line_no": "1", "body": "x"},
        auth=ALEX,
    )
    assert response.status_code == 404
    assert not (docs / "mia/secret.comments.jsonl").exists()


@pytest.mark.parametrize(
    ("form", "error"),
    [
        ({"line_no": "-1", "body": "x"}, "The line is not in the document."),
        ({"line_no": "999", "body": "x"}, "The line is not in the document."),
        ({"line_no": "nope", "body": "x"}, "The line is not in the document."),
        ({"line_no": "1", "body": "  "}, "The comment is empty."),
        ({"line_no": "1", "body": "x" * 2001}, "The comment exceeds 2000 characters."),
    ],
)
def test_a_bad_comment_is_refused(docs, form, error) -> None:
    c = client()
    token = _csrf(c.get("/documents/alex/party", auth=ALEX).text)
    response = c.post("/documents/alex/party/comments", data=form | {"csrf": token}, auth=ALEX)
    assert response.status_code == 400
    assert error in response.text
    assert not (docs / "alex/party.comments.jsonl").exists()


def test_a_comment_on_line_0_takes_the_title_line(docs) -> None:
    c = client()
    token = _csrf(c.get("/documents/alex/party", auth=ALEX).text)
    response = c.post(
        "/documents/alex/party/comments",
        data={"csrf": token, "line_no": "0", "body": "Shorter title"},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/documents/alex/party#L0"
    record = json.loads((docs / "alex/party.comments.jsonl").read_text())
    assert record["line_no"] == 0 and record["line_text"] == "title: Party plan"


def test_a_resolved_comment_on_the_document_says_so() -> None:
    done = ds.DocComment(id=1, line_no=0, line_text="title: A", resolved_at="x", resolution="ok")
    assert "The document: <code>title: A</code>" in vc.resolved_html([done], tz=UTC, now=NOW)


def test_a_comment_with_no_repository_is_stored_with_no_commit(data) -> None:
    _write(data / "documents/alex/note.md", "---\ntitle: Note\n---\nHello\n")
    c = client()
    token = _csrf(c.get("/documents/alex/note", auth=ALEX).text)
    response = c.post(
        "/documents/alex/note/comments",
        data={"csrf": token, "line_no": "1", "body": "x"},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    record = json.loads((data / "documents/alex/note.comments.jsonl").read_text())
    assert record["line_text"] == "Hello"
    assert not (data / "documents/.git").exists()


# -- versions ---------------------------------------------------------------


@NEEDS_GIT
def test_the_versions_and_an_old_version(docs) -> None:
    _write(docs / "alex/party.md", PLAN.replace("Book the music", "Book the band"))
    wikigit.commit(docs, [docs / "alex/party.md"], "write_document: documents/alex/party.md")
    html = _main(_get("/documents/alex/party").text)
    assert "2 versions" in html
    versions = ds.versions(docs, ds.find(docs, "alex", "party", ["alex"]))
    old = versions[1].sha
    assert f'href="/documents/alex/party?rev={old}"' in html
    page = _main(_get("/documents/alex/party", rev=old).text)
    assert "Book the music" in page and "Book the band" not in page
    assert "It is read-only." in page
    assert "a-comment-form" not in page and 'class="a-add"' not in page
    # A prefix of a version works; any other value is 404.
    assert _get("/documents/alex/party", rev=old[:10]).status_code == 200
    assert _get("/documents/alex/party", rev="HEAD").status_code == 404
    assert _get("/documents/alex/party", rev="0" * 40).status_code == 404


@NEEDS_GIT
def test_a_version_of_another_file_is_404(docs) -> None:
    _write(docs / "alex/other.md", "---\ntitle: Other\n---\nx\n")
    wikigit.commit(docs, [docs / "alex/other.md"], "write_document: other")
    other = ds.versions(docs, ds.find(docs, "alex", "other", ["alex"]))[0].sha
    assert _get("/documents/alex/party", rev=other).status_code == 404


def test_no_repository_gives_no_version_history(data) -> None:
    _write(data / "documents/alex/note.md", "---\ntitle: Note\n---\nHello\n")
    assert "No version history." in _get("/documents/alex/note").text
    assert _get("/documents/alex/note", rev="abcdef1").status_code == 404


# -- an HTML document -------------------------------------------------------

HTML_DOC = textwrap.dedent("""\
    ---
    title: Mockup
    kind: artifact
    status: active
    ---
    <h1>Hi "there"</h1><script>parent.document.title='x'</script>
    """)


def _frame(html: str) -> str:
    return html[html.index("<iframe") : html.index("</iframe>")]


def test_an_html_document_is_in_a_sandbox_with_no_scripts(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    html = _main(_get("/documents/alex/mockup").text)
    frame = _frame(html)
    assert 'sandbox=""' in frame
    assert "allow-scripts" not in frame and "allow-same-origin" not in frame
    assert "srcdoc=" in frame
    assert "<script>" not in frame and "&lt;script&gt;" in frame
    assert "&quot;there&quot;" in frame
    # The frontmatter is not in the frame.
    assert "title: Mockup" not in frame
    assert "Scripts of this document do not run." in html
    assert '<details class="a-source"><summary>Source</summary>' in html
    assert "&lt;h1&gt;Hi" in html[html.index('class="a-source"') :]


@pytest.mark.parametrize(
    ("value", "sandbox"), [("true", "allow-scripts"), ("yes-please", ""), ("false", "")]
)
def test_only_scripts_true_allows_scripts(docs, value, sandbox) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC.replace("status: active", f"scripts: {value}"))
    frame = _frame(_get("/documents/alex/mockup").text)
    assert f'sandbox="{sandbox}"' in frame
    assert "allow-same-origin" not in frame


def test_the_policy_is_the_same_on_a_document_page(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    response = _get("/documents/alex/mockup")
    assert response.headers["content-security-policy"] == (
        f"default-src 'none'; img-src 'self'; style-src 'self'; script-src '{sc.COPY_SCRIPT_HASH}'"
        "; frame-ancestors 'self'"
    )
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_an_html_document_lists_as_html(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    html = _main(_get("/documents").text)
    assert "Mockup" in html and "html" in html


# -- the assignment link ----------------------------------------------------


def test_an_output_of_kind_document_links_to_its_page() -> None:
    card = sc.output_card(sc.Card("document", "documents/alex/party.md", "Party plan"))
    assert '<a class="a-out-title" href="/documents/alex/party">Party plan</a>' in card
    other = sc.output_card(sc.Card("document", "documents/../wiki/x.md", "Bad"))
    assert "href=" not in other
    page = sc.output_card(sc.Card("page", "documents/alex/party.md", "Not a document"))
    assert "href=" not in page


@pytest.mark.parametrize(
    ("ref", "url"),
    [
        ("documents/alex/party.md", "/documents/alex/party"),
        ("/documents/shared/x.html", "/documents/shared/x"),
        ("documents/alex/a/b.md", None),
        ("documents/Alex/party.md", None),
        ("documents/alex/.x.md", None),
        ("documents/alex/party.txt", None),
        ("wiki/alex/party.md", None),
    ],
)
def test_document_url(ref, url) -> None:
    assert ds.document_url(ref) == url


# -- the store --------------------------------------------------------------


def test_split_counts_the_lines_before_the_body() -> None:
    front, body, offset = ds.split("---\ntitle: A\n---\nBody\n")
    assert front == {"title": "A"} and body == "Body\n" and offset == 3
    assert ds.split("No frontmatter") == ({}, "No frontmatter", 0)
    assert ds.split("---\nnot closed\n") == ({}, "---\nnot closed\n", 0)
    assert ds.split("---\n- a list\n---\nx")[0] == {}
    assert ds.split("---\n: bad: yaml: [\n---\nx")[2] == 0


def test_the_defaults_of_the_frontmatter(data) -> None:
    _write(data / "documents/alex/bare.md", "# Heading\n\nThe first paragraph\ngoes on.\n")
    doc = ds.find(data / "documents", "alex", "bare", ["alex"])
    assert doc is not None
    assert (doc.title, doc.kind, doc.status, doc.format) == ("bare", "note", "draft", "markdown")
    assert doc.summary == "The first paragraph goes on."
    assert doc.updated is not None and not doc.scripts


def test_the_list_skips_comments_files_and_hidden_files(docs) -> None:
    _write(docs / "alex/party.comments.jsonl", "")
    _write(docs / "alex/.trash/x.md", "---\ntitle: Trashed\n---\n")
    _write(docs / ".trash/20260101T000000Z/alex/y.md", "---\ntitle: Old\n---\n")
    titles = [d.title for d in ds.list_documents(docs, ds.owners("alex"))]
    assert sorted(titles) == ["Docs cleanup", "Meal plan", "Party plan"]


def test_a_new_comment_id_follows_the_largest() -> None:
    def c(i):
        return ds.DocComment(id=i, line_no=1)

    assert ds._next_id([]) == 1
    assert ds._next_id([c(1), c(7)]) == 8
    assert ds._next_id([c("a1b2"), c(2)]) == 3
    assert ds._next_id([c("1")]) == 2


def test_the_row_and_the_page_titles(docs) -> None:
    doc = ds.find(docs, "shared", "meals", ["alex", "shared"])
    assert doc is not None
    row = dm.row_html(doc, person_id="alex", comments=0, tz=UTC, now=NOW)
    assert "shared" in row and escape("Meal plan") in row
    assert dm.parse_kind("plan") == "plan" and dm.parse_kind("<x>") is None


def test_the_documents_fit_a_phone() -> None:
    css = (Path(viewer.__file__).parent / "viewer.css").read_text()
    rule = css[css.index("iframe.d-frame {") :][:300]
    assert "width: 100%" in rule and "max-width: 100%" in rule
    assert ".a-card-unit > details.a-add[open], .a-card-unit > .a-comment" in css
    phone = css[css.index("@media (max-width: 560px)") :]
    assert "iframe.d-frame { min-height: 60vh; }" in phone
    assert "overflow-wrap: anywhere" in css[css.index("ol.d-versions") :][:200]


def test_the_time_of_a_file_is_used_with_no_updated(data) -> None:
    path = data / "documents/alex/t.md"
    _write(path, "x")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    doc = ds.find(data / "documents", "alex", "t", ["alex"])
    assert doc is not None and doc.updated == NOW.isoformat()

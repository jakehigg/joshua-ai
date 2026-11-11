"""The documents of the viewer: the two routes the chat calls, the store, and
the gutter of a document.

The React app renders the list and the page, and ``viewer_api.documents``
answers it; ``test_viewer_api_documents.py`` covers those routes. What stays
here is the frontmatter as JSON, the body of an HTML document with its own
content policy, the rules of the store, and the stylesheet.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_comments as vc
from joshua_gateway import viewer_docstore as ds
from joshua_gateway import viewer_documents as dm
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


# -- the nav and access -----------------------------------------------------


def test_the_nav_has_documents_for_a_member(data) -> None:
    html = _get("/assignments").text
    assert 'href="/documents">' in html and ">Documents</a>" in html


def test_a_guest_has_no_documents_section(docs) -> None:
    html = _get("/assignments", auth=GUEST).text
    assert 'href="/documents"' not in html
    assert _get("/documents/shared/meals.json", auth=GUEST).status_code == 404
    assert _get("/documents/shared/meals/artifact", auth=GUEST).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/documents/alex/.hidden.json",
        "/documents/alex/party.md.json",
        "/documents/alex/party.comments.json",
        "/documents/Alex/party.json",
        "/documents/wiki/index.json",
    ],
)
def test_a_bad_path_is_404(docs, path) -> None:
    assert _get(path).status_code == 404


def test_a_link_out_of_the_folder_is_404(docs, tmp_path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: Outside\n---\nNo.\n")
    (docs / "alex/link.md").symlink_to(outside)
    assert _get("/documents/alex/link.json").status_code == 404


# -- the comments of a document ---------------------------------------------


def test_a_comment_on_the_document_is_stale_when_the_title_changes(docs) -> None:
    line = {"id": 1, "line_no": 0, "line_text": "title: Old plan", "body": "x", "author": "a"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line) + "\n")
    doc = ds.find(docs, "alex", "party", ["alex"])
    assert doc is not None and [c.stale for c in ds.read_comments(doc)] == [True]


def test_a_frontmatter_change_never_moves_a_comment(docs) -> None:
    line = {"id": 1, "line_no": 5, "line_text": "- Order the food", "body": "x", "author": "a"}
    _write(docs / "alex/party.comments.jsonl", json.dumps(line) + "\n")
    _write(docs / "alex/party.md", PLAN.replace("kind: plan\n", "kind: plan\ntags:\n- a\n- b\n"))
    doc = ds.find(docs, "alex", "party", ["alex"])
    assert doc is not None and [c.stale for c in ds.read_comments(doc)] == [False]


def test_a_bad_comments_file_line_is_skipped(docs) -> None:
    _write(docs / "alex/party.comments.jsonl", "not json\n[1]\n{}\n")
    doc = ds.find(docs, "alex", "party", ["alex"])
    assert doc is not None and ds.read_comments(doc) == []


def test_a_resolved_comment_on_the_document_says_so() -> None:
    done = ds.DocComment(id=1, line_no=0, line_text="title: A", resolved_at="x", resolution="ok")
    assert "The document: <code>title: A</code>" in vc.resolved_html([done], tz=UTC, now=NOW)


# -- the frontmatter as JSON ------------------------------------------------


def test_the_json_of_a_document_gives_four_fields_and_no_body(docs) -> None:
    response = _get("/documents/alex/party.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "title": "Party plan",
        "kind": "plan",
        "status": "active",
        "updated": "2026-11-09T12:00:00+00:00",
    }
    assert "Food and music" not in response.text


def test_the_json_needs_a_sign_in(docs) -> None:
    assert client().get("/documents/alex/party.json").status_code == 401


def test_the_json_of_a_document_that_is_not_there_is_404(docs) -> None:
    response = _get("/documents/alex/nothing.json")
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_the_json_of_another_members_document_is_404(docs) -> None:
    assert _get("/documents/mia/secret.json").status_code == 404
    assert _get("/documents/mia/secret.json", auth=MIA).status_code == 200
    assert _get("/documents/shared/meals.json").status_code == 200
    assert _get("/documents/shared/meals.json", auth=GUEST).status_code == 404


def test_a_slug_that_ends_in_json_is_not_a_document(docs) -> None:
    _write(docs / "alex/data.json.md", "---\ntitle: Data\nstatus: active\n---\nRows.\n")
    assert ds.valid_slug("data.json") is False
    # The address names the frontmatter of ``data``, which is not there.
    assert _get("/documents/alex/data.json").status_code == 404
    assert _get("/documents/alex/data.json.json").status_code == 404
    assert [d.title for d in ds.list_documents(docs, ds.owners("alex"))].count("Data") == 0


# -- the artifact of an HTML document ---------------------------------------

HTML_DOC = textwrap.dedent("""\
    ---
    title: Mockup
    kind: artifact
    status: active
    ---
    <style>h1 { color: red }</style><h1>Hi "there"</h1><script>parent.document.title='x'</script>
    """)


def test_the_artifact_route_serves_the_body_with_its_own_policy(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    response = _get("/documents/alex/mockup/artifact")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The body only: the frontmatter stays out of the frame.
    assert "<style>h1 { color: red }</style>" in response.text
    assert 'Hi "there"' in response.text
    assert "title: Mockup" not in response.text
    policy = response.headers["content-security-policy"]
    assert policy == (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        "form-action 'none'; frame-src 'none'; frame-ancestors 'self'; sandbox"
    )
    # Styles render, and nothing else loads: no script, no network, no form.
    assert "script-src" not in policy
    assert "connect-src" not in policy
    assert "allow-same-origin" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_the_artifact_policy_allows_a_script_only_with_scripts_true(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC.replace("status: active", "scripts: true"))
    policy = _get("/documents/alex/mockup/artifact").headers["content-security-policy"]
    assert policy == (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; form-action 'none'; frame-src 'none'; "
        "frame-ancestors 'self'; sandbox allow-scripts"
    )
    assert "connect-src" not in policy
    assert "allow-same-origin" not in policy


def test_the_artifact_route_has_the_rules_of_the_page(docs) -> None:
    _write(docs / "alex/mockup.html", HTML_DOC)
    _write(docs / "mia/private.html", HTML_DOC)
    # A sign-in, the owner rule, a guest, and a document that is not there.
    assert client().get("/documents/alex/mockup/artifact").status_code == 401
    assert _get("/documents/mia/private/artifact").status_code == 404
    assert _get("/documents/alex/mockup/artifact", auth=GUEST).status_code == 404
    assert _get("/documents/alex/none/artifact").status_code == 404
    # A Markdown document has no frame, so it has no artifact.
    assert _get("/documents/alex/party/artifact").status_code == 404


@NEEDS_GIT
def test_the_artifact_route_serves_an_older_version(docs) -> None:
    path = docs / "alex/mockup.html"
    _write(path, HTML_DOC)
    wikigit.commit(docs, [path], "write_document: mockup")
    old = ds.versions(docs, ds.find(docs, "alex", "mockup", ["alex"]))[0].sha
    _write(path, HTML_DOC.replace("Hi", "Hello"))
    wikigit.commit(docs, [path], "write_document: mockup")
    assert "Hi" in _get("/documents/alex/mockup/artifact", rev=old).text
    assert "Hello" in _get("/documents/alex/mockup/artifact").text
    assert _get("/documents/alex/mockup/artifact", rev="0" * 40).status_code == 404


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


def test_the_marks_of_a_document(docs) -> None:
    doc = ds.find(docs, "shared", "meals", ["alex", "shared"])
    assert doc is not None and doc.title == "Meal plan"
    assert dm.owner_label("shared", "alex") == "shared"
    assert dm.owner_label("alex", "alex") == "yours"
    assert dm.parse_kind("plan") == "plan" and dm.parse_kind("<x>") is None
    assert dm.parse_status("final") == "final" and dm.parse_status("<x>") == dm.DEFAULT_STATUS


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

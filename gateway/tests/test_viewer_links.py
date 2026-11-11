"""Offline tests for the viewer's links: the file, the form, the page and
the navigation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_links as links
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
    """)

PANTRY = "https://pantry.example.test/"


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    (data / "wiki/Home.md").write_text("# Home\n\nThe front door.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return data


@pytest.fixture
def client(data) -> TestClient:
    return TestClient(viewer.build_app())


def _links_file(data: Path) -> Path:
    return data / "wiki/.viewer/links.yaml"


def _seed(data: Path, text: str) -> None:
    path = _links_file(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _post(client, fields: dict[str, str], auth=ALEX, csrf: str | None = None):
    token = csrf if csrf is not None else viewer._csrf_token(auth[0], "links")
    return client.post("/links", data={"csrf": token, **fields}, auth=auth, follow_redirects=False)


# -- checking a link ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "/wiki/",
        "ftp://files.example.test/",
        "https://",
        "https://exa mple.test/",
        "https://example.test/" + "a" * links.MAX_URL,
    ],
)
def test_a_url_that_is_not_an_http_page_is_refused(url):
    with pytest.raises(links.LinkError):
        links.check_url(url)


def test_an_http_url_is_kept_as_written():
    assert links.check_url("  http://192.168.1.20:8080/x?y=1  ") == "http://192.168.1.20:8080/x?y=1"


def test_a_link_needs_a_short_title_and_an_unknown_icon_becomes_link():
    with pytest.raises(links.LinkError):
        links.check_link("   ", PANTRY, "box")
    with pytest.raises(links.LinkError):
        links.check_link("x" * (links.MAX_TITLE + 1), PANTRY, "box")
    link = links.check_link("  Pantry   app ", PANTRY, "no-such-icon")
    assert link == links.Link("Pantry app", PANTRY, "link")


# -- the file -------------------------------------------------------------------


def test_the_file_round_trips():
    saved = [
        links.Link("Pantry", PANTRY, "box"),
        links.Link("Vinyl", "http://vinyl.test/", "music"),
    ]
    assert links.parse(links.dump(saved)) == saved


def test_a_bad_entry_is_skipped_and_the_rest_still_show():
    text = textwrap.dedent(f"""\
        links:
          - title: Pantry
            url: {PANTRY}
            icon: box
          - title: Bad
            url: javascript:alert(1)
          - just a string
          - title: Tasks
            url: https://tasks.example.test/
        """)
    assert [link.title for link in links.parse(text)] == ["Pantry", "Tasks"]


@pytest.mark.parametrize("text", ["", "links: [", "- a list\n", "links: nope\n"])
def test_a_broken_file_shows_no_links(text):
    assert links.parse(text) == []


def test_a_missing_file_shows_no_links(tmp_path):
    assert links.load(tmp_path) == []


# -- the navigation -------------------------------------------------------------


def test_the_sidebar_shows_each_link_in_a_new_tab(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n    icon: box\n")
    html = client.get("/assignments", auth=ALEX).text
    nav = html[html.index('aria-label="Links"') :]
    nav = nav[: nav.index("</nav>")]
    assert f'href="{PANTRY}" target="_blank" rel="noopener noreferrer"' in nav
    assert "<span>Pantry</span>" in nav
    assert 'href="/links">Edit links</a>' in nav


def test_with_no_links_the_sidebar_offers_to_add_them(client):
    html = client.get("/assignments", auth=ALEX).text
    assert 'href="/links">Add links</a>' in html


def test_a_phone_gets_a_links_tab(client):
    html = client.get("/assignments", auth=ALEX).text
    assert '<a class="j-tab" href="/links">Links</a>' in html


def test_titles_and_urls_are_escaped(client, data):
    _seed(
        data,
        'links:\n  - title: "<b>x</b>"\n    url: "https://example.test/?a=1&b=\\"2\\""\n',
    )
    html = client.get("/links", auth=ALEX).text
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert 'href="https://example.test/?a=1&amp;b=&quot;2&quot;"' in html


# -- the page -------------------------------------------------------------------


def test_a_member_sees_the_list_and_the_form(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n    icon: box\n")
    html = client.get("/links", auth=ALEX).text
    assert 'class="j-tab" href="/links" aria-current="page"' in html
    assert '<span class="j-list-title">Pantry</span>' in html
    assert 'action="/links" method="post"' in html
    assert f'name="url-0" value="{PANTRY}"' in html
    assert '<option value="box" selected>box</option>' in html
    # three blank rows for new links
    assert 'name="title-3"' in html and 'name="title-4"' not in html


def test_a_guest_sees_the_list_but_no_form(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n")
    html = client.get("/links", auth=MIA).text
    assert "Pantry" in html
    assert '<form class="v-links-form"' not in html


# -- saving ---------------------------------------------------------------------


def test_saving_adds_edits_and_removes_links(client, data):
    _seed(
        data,
        f"links:\n  - title: Pantry\n    url: {PANTRY}\n  - title: Old\n    url: https://old.test/\n",
    )
    response = _post(
        client,
        {
            "title-0": "Pantry",
            "url-0": "https://pantry.example.test/inventory",
            "icon-0": "cart",
            "title-1": "Old",
            "url-1": "https://old.test/",
            "remove-1": "1",
            "title-2": "Vinyl",
            "url-2": "http://vinyl.test:8080/",
            "icon-2": "music",
            "title-3": "",
            "url-3": "",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/links"
    assert links.load(data / "wiki") == [
        links.Link("Pantry", "https://pantry.example.test/inventory", "cart"),
        links.Link("Vinyl", "http://vinyl.test:8080/", "music"),
    ]
    assert _links_file(data).read_text().startswith("# The links in the viewer's navigation.")


def test_a_bad_row_saves_nothing_and_says_which_row(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n")
    before = _links_file(data).read_text()
    response = _post(
        client, {"title-0": "Pantry", "url-0": PANTRY, "title-1": "X", "url-1": "javascript:1"}
    )
    assert response.status_code == 400
    assert "row 2:" in response.text
    assert _links_file(data).read_text() == before


def test_a_guest_cannot_save(client, data):
    response = _post(client, {"title-0": "Pantry", "url-0": PANTRY}, auth=MIA)
    assert response.status_code == 403
    assert not _links_file(data).exists()


def test_a_save_needs_the_token_and_the_same_origin(client, data):
    assert _post(client, {"title-0": "P", "url-0": PANTRY}, csrf="nope").status_code == 403
    token = viewer._csrf_token("alex", "links")
    response = client.post(
        "/links",
        data={"csrf": token, "title-0": "P", "url-0": PANTRY},
        auth=ALEX,
        headers={"origin": "https://evil.example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert not _links_file(data).exists()


def test_the_links_file_is_hidden_from_the_wiki(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n")
    folder = client.get("/api/v1/wiki?path=wiki", auth=ALEX)
    assert ".viewer" not in folder.text
    assert client.get("/api/v1/files/wiki/.viewer/links.yaml", auth=ALEX).status_code == 404

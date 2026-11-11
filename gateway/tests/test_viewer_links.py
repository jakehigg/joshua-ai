"""Offline tests for the viewer's links: the checks, the file, the rows of a
write, and the links group of the server-rendered sidebar.

``test_viewer_api_links`` covers the JSON routes the page and the sidebar of
the React app read."""

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


def test_titles_and_urls_are_escaped_in_the_sidebar(client, data):
    _seed(
        data,
        'links:\n  - title: "<b>x</b>"\n    url: "https://example.test/?a=1&b=\\"2\\""\n',
    )
    html = client.get("/assignments", auth=ALEX).text
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert 'href="https://example.test/?a=1&amp;b=&quot;2&quot;"' in html


# -- the rows of a write --------------------------------------------------------


def test_the_rows_of_a_write_keep_their_order_and_drop_a_blank_one():
    rows = [
        {"title": "Pantry", "url": PANTRY, "icon": "box"},
        {"title": "  ", "url": "  ", "icon": "link"},
        {"title": "Vinyl", "url": "http://vinyl.test:8080/", "icon": "music"},
    ]
    assert links.from_rows(rows) == [
        links.Link("Pantry", PANTRY, "box"),
        links.Link("Vinyl", "http://vinyl.test:8080/", "music"),
    ]


@pytest.mark.parametrize(
    "row",
    [
        {"title": "X", "url": "javascript:alert(1)", "icon": "link"},
        {"title": "", "url": PANTRY, "icon": "link"},
        {"title": "X", "url": PANTRY, "icon": "no-such-icon"},
        "just a string",
    ],
)
def test_a_bad_row_names_the_row(row):
    with pytest.raises(links.LinkError, match="row 2"):
        links.from_rows([{"title": "Pantry", "url": PANTRY, "icon": "box"}, row])


def test_a_write_that_carries_no_list_is_refused():
    with pytest.raises(links.LinkError):
        links.from_rows(None)


def test_a_write_keeps_at_most_the_cap():
    rows = [{"title": f"L{i}", "url": f"https://x{i}.test/"} for i in range(links.MAX_LINKS + 1)]
    with pytest.raises(links.LinkError, match=str(links.MAX_LINKS)):
        links.from_rows(rows)


# -- the file is not of the wiki ------------------------------------------------


def test_the_links_file_is_hidden_from_the_wiki(client, data):
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n")
    folder = client.get("/api/v1/wiki?path=wiki", auth=ALEX)
    assert ".viewer" not in folder.text
    assert client.get("/api/v1/files/wiki/.viewer/links.yaml", auth=ALEX).status_code == 404

"""Offline tests for the viewer's journal feed, day page, and edit form.

The feed reads ``wiki/journal/YYYY/MM/DD/*.md`` from a temp data root. The
same fixture shape as ``test_viewer.py``: a temp ``joshua.yaml``, passwords
in the environment, the Starlette ``TestClient`` in-process.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from datetime import date
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_journal
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

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

DAY_PAGE = textwrap.dedent("""\
    ---
    date: 2026-09-04
    people:
    - alex
    - mia
    source: nightly
    ---

    Alex booked the flights to Lisbon for October. Mia asked about the
    ferry schedule and Joshua found the Thursday sailing.
    """)

ENTRY = textwrap.dedent("""\
    ---
    date: 2026-09-04
    people:
    - alex
    source: agent
    ---

    # Oatmeal for breakfast

    Alex had oatmeal, with the good honey.
    """)

OLDER = textwrap.dedent("""\
    ---
    date: 2026-08-20
    people:
    - mia
    source: agent
    ---

    Mia started the pottery class on Wednesdays.
    """)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    j = data / "wiki" / "journal"
    _write(j / "2026/09/04/2026-09-04.md", DAY_PAGE)
    _write(j / "2026/09/04/oatmeal-breakfast.md", ENTRY)
    _write(j / "2026/08/20/pottery-class.md", OLDER)
    _write(j / "legacy/alex/old.md", "# Old\n\nFrom before the single wiki.\n")
    _write(j / "2026/09/04/.draft.md", "# Hidden\n\nNever shown.\n")
    _write(data / "wiki/pizza.md", "# Pizza\n\nNot a journal page.\n")


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    data.mkdir()
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return TestClient(viewer.build_app())


# -- the model --------------------------------------------------------------


def test_entries_load_newest_day_first_and_day_page_first(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    entries = viewer_journal.load_entries(data)
    assert [e.slug for e in entries] == ["2026-09-04", "oatmeal-breakfast", "pottery-class"]
    assert entries[0].is_day_page and entries[0].source == "nightly"
    assert entries[0].title == viewer_journal.DAY_PAGE_TITLE
    assert entries[1].title == "Oatmeal for breakfast"
    assert entries[1].people == ("alex",)
    assert "# Oatmeal" not in entries[1].body


def test_legacy_and_hidden_files_are_not_in_the_feed(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    slugs = {e.slug for e in viewer_journal.load_entries(data)}
    assert "old" not in slugs
    assert ".draft" not in slugs


def test_months_and_month_parsing(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    entries = viewer_journal.load_entries(data)
    assert viewer_journal.months(entries) == [(2026, 9), (2026, 8)]
    assert viewer_journal.parse_month("2026-09") == (2026, 9)
    assert viewer_journal.parse_month("2026-13") is None
    assert viewer_journal.parse_month("sept") is None


def test_a_legacy_person_key_names_the_person():
    entry = viewer_journal.parse_entry(
        "journal/2026/08/01/alex.md", date(2026, 8, 1), "---\nperson: alex\n---\n\nText.\n"
    )
    assert entry.people == ("alex",)


def test_frontmatter_round_trips():
    front, body = viewer_journal.split_frontmatter(ENTRY)
    assert front["date"] == date(2026, 9, 4)
    text = viewer_journal.render_frontmatter(front)
    assert text.startswith("---\ndate: 2026-09-04\npeople:\n- alex\nsource: agent\n---\n\n")


# -- the feed ---------------------------------------------------------------


def test_feed_needs_auth(client):
    assert client.get("/journal").status_code == 401


def test_feed_shows_the_newest_month_with_days_and_entries(client):
    html = client.get("/journal", auth=ALEX).text
    assert "September 2026" in html
    assert "Friday 4 September 2026" in html
    assert "Oatmeal for breakfast" in html
    assert "flights to Lisbon" in html
    assert 'class="badge nightly"' in html
    assert "pottery" not in html  # August is another month
    assert "← August 2026" in html


def test_feed_month_query_picks_the_month(client):
    html = client.get("/journal?month=2026-08", auth=ALEX).text
    assert "pottery class" in html
    assert "Oatmeal" not in html
    assert "September 2026 →" in html


def test_feed_person_filter_keeps_entries_naming_that_person(client):
    html = client.get("/journal?person=mia", auth=ALEX).text
    assert "flights to Lisbon" in html  # the day page names mia
    assert "Oatmeal" not in html  # names alex only
    assert 'class="chip on"' in html


def test_feed_unknown_person_filter_is_ignored(client):
    html = client.get("/journal?person=nobody", auth=ALEX).text
    assert "Oatmeal" in html


def test_feed_empty_month_says_so(client):
    html = client.get("/journal?month=2020-01", auth=ALEX).text
    assert "Nothing in the journal yet" in html


def test_empty_journal_is_a_page_not_an_error(client, tmp_path):
    shutil.rmtree(tmp_path / "data" / "wiki" / "journal")
    response = client.get("/journal", auth=ALEX)
    assert response.status_code == 200
    assert "Nothing in the journal yet" in response.text


def test_raw_html_in_an_entry_is_escaped(client, tmp_path):
    _write(
        tmp_path / "data/wiki/journal/2026/09/04/raw.md",
        "---\ndate: 2026-09-04\npeople: []\nsource: agent\n---\n\n<script>alert(1)</script>\n",
    )
    html = client.get("/journal", auth=ALEX).text
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_home_and_nav_link_to_the_journal(client):
    assert 'href="/journal"' in client.get("/", auth=ALEX).text
    assert 'href="/journal"' in client.get("/wiki/pizza.md", auth=ALEX).text


# -- the day page -----------------------------------------------------------


def test_day_page_shows_the_day_and_its_neighbours(client):
    html = client.get("/journal/2026/09/04", auth=ALEX).text
    assert "Friday 4 September 2026" in html
    assert "Oatmeal for breakfast" in html
    assert "← Thursday 20 August 2026" in html
    assert "→" not in html  # nothing newer


def test_day_page_for_an_empty_day(client):
    html = client.get("/journal/2026/09/01", auth=ALEX).text
    assert "Nothing on this day." in html
    assert "Friday 4 September 2026 →" in html


def test_day_page_with_a_bad_date_is_404(client):
    assert client.get("/journal/2026/02/31", auth=ALEX).status_code == 404


# -- edit -------------------------------------------------------------------


def test_member_sees_edit_link_and_guest_does_not(client):
    assert 'href="/edit/' in client.get("/journal", auth=ALEX).text
    assert 'href="/edit/' not in client.get("/journal", auth=MIA).text


def test_edit_form_shows_body_and_people(client):
    html = client.get("/edit/journal/2026/09/04/oatmeal-breakfast.md", auth=ALEX).text
    assert 'value="alex"' in html
    assert "with the good honey" in html
    assert "# Oatmeal" not in html  # the title heading is not part of the text field


def test_guest_cannot_open_edit_or_save(client):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    assert client.get(f"/edit/{rel}", auth=MIA).status_code == 403
    token = viewer._csrf_token("mia", "wiki/" + rel)
    response = client.post("/save", data={"path": rel, "csrf": token, "body": "x"}, auth=MIA)
    assert response.status_code == 403


def test_member_saves_text_and_people_and_keeps_the_rest(client, tmp_path):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    response = client.post(
        "/save",
        data={"path": rel, "csrf": token, "people": "alex, mia", "body": "Alex had porridge.\n"},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/journal/2026/09/04#oatmeal-breakfast"
    text = (tmp_path / "data/wiki" / rel).read_text()
    assert text == (
        "---\ndate: 2026-09-04\npeople:\n- alex\n- mia\nsource: agent\n---\n\n"
        "# Oatmeal for breakfast\n\nAlex had porridge.\n"
    )


def test_saving_a_day_page_keeps_it_a_day_page(client, tmp_path):
    rel = "journal/2026/09/04/2026-09-04.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    client.post(
        "/save",
        data={"path": rel, "csrf": token, "people": "alex", "body": "Only the flights.\r\n"},
        auth=ALEX,
        follow_redirects=False,
    )
    text = (tmp_path / "data/wiki" / rel).read_text()
    assert (
        text
        == "---\ndate: 2026-09-04\npeople:\n- alex\nsource: nightly\n---\n\nOnly the flights.\n"
    )


def test_save_with_a_bad_person_id_is_400(client, tmp_path):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    before = (tmp_path / "data/wiki" / rel).read_text()
    response = client.post(
        "/save",
        data={"path": rel, "csrf": token, "people": "Not A Person", "body": "x"},
        auth=ALEX,
    )
    assert response.status_code == 400
    assert (tmp_path / "data/wiki" / rel).read_text() == before


def test_save_refuses_a_legacy_entry_a_hidden_one_and_a_bad_csrf(client, tmp_path):
    for rel in (
        "journal/legacy/alex/old.md",
        "../people/alex/x.md",
        "journal/2026/09/04/.draft.md",
    ):
        token = viewer._csrf_token("alex", "wiki/" + rel)
        response = client.post("/save", data={"path": rel, "csrf": token, "body": "x"}, auth=ALEX)
        assert response.status_code in (403, 404), rel
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    response = client.post("/save", data={"path": rel, "csrf": "bad", "body": "x"}, auth=ALEX)
    assert response.status_code == 403


def test_save_with_a_bad_origin_is_403(client):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    response = client.post(
        "/save",
        data={"path": rel, "csrf": token, "body": "x"},
        headers={"Origin": "http://evil.example"},
        auth=ALEX,
    )
    assert response.status_code == 403


def test_edit_of_a_missing_entry_is_404(client):
    assert client.get("/edit/journal/2026/09/04/nope.md", auth=ALEX).status_code == 404


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_save_commits_the_edit(client, tmp_path):
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    client.post(
        "/save",
        data={"path": rel, "csrf": token, "people": "alex", "body": "Porridge."},
        auth=ALEX,
        follow_redirects=False,
    )
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert f"viewer: edit {rel}" in log


def test_a_table_renders_in_an_entry_and_a_wiki_page(client, tmp_path):
    table = "| reading | inches |\n|---|---|\n| morning | 0.8 |\n"
    _write(
        tmp_path / "data/wiki/journal/2026/09/04/rain.md",
        "---\ndate: 2026-09-04\npeople: [alex]\nsource: agent\n---\n\n" + table,
    )
    _write(tmp_path / "data/wiki/table.md", "# Table\n\n" + table)
    for url in ("/journal", "/wiki/table.md"):
        html = client.get(url, auth=ALEX).text
        assert "<table>" in html and "<th>reading</th>" in html and "<td>0.8</td>" in html, url

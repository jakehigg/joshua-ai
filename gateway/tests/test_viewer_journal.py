"""Offline tests for the journal's model and the edit of an entry.

The model reads ``wiki/journal/YYYY/MM/DD/*.md`` from a temp data root. The
same fixture shape as ``test_viewer.py``: a temp ``joshua.yaml``, passwords
in the environment, the Starlette ``TestClient`` in-process. The JSON routes
of the feed are in ``test_viewer_api_journal.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from datetime import date
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_edit, viewer_journal
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
    text = viewer_edit.render_frontmatter(front)
    assert text.startswith("---\ndate: 2026-09-04\npeople:\n- alex\nsource: agent\n---\n\n")


def test_the_days_of_a_month_and_the_addresses_of_the_feed(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    entries = viewer_journal.load_entries(data)
    assert viewer_journal.days(entries) == [date(2026, 9, 4), date(2026, 8, 20)]
    assert viewer_journal.day_url(date(2026, 9, 4)) == "/journal/2026/09/04"
    assert viewer_journal.month_url((2026, 9)) == "/journal?month=2026-09"
    assert viewer_journal.month_url((2026, 9), "mia") == "/journal?month=2026-09&person=mia"


def test_the_nav_of_a_page_and_of_the_app_links_to_the_journal(client):
    assert 'href="/journal"' in client.get("/assignments/saved", auth=ALEX).text
    sections = client.get("/api/v1/session", auth=ALEX).json()["sections"]
    assert "/journal" in [section["path"] for section in sections]


# -- edit -------------------------------------------------------------------


def test_the_editor_of_an_entry_carries_the_text_and_the_people(client):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    body = client.get("/api/v1/edit", params={"path": rel}, auth=ALEX).json()
    assert body["kind"] == "journal" and body["people"] == ["alex"]
    assert "with the good honey" in body["body"]
    assert "# Oatmeal" not in body["body"]  # the title heading is not part of the text


def test_guest_cannot_open_edit_or_save(client):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    assert client.get("/api/v1/edit", params={"path": rel}, auth=MIA).status_code == 403
    token = viewer._csrf_token("mia", "wiki/" + rel)
    fields = {"path": rel, "csrf": token, "body": "x"}
    assert client.post("/api/v1/save", json=fields, auth=MIA).status_code == 403


def test_member_saves_text_and_people_and_keeps_the_rest(client, tmp_path):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    response = client.post(
        "/api/v1/save",
        json={"path": rel, "csrf": token, "people": "alex, mia", "body": "Alex had porridge.\n"},
        auth=ALEX,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/journal/2026/09/04#oatmeal-breakfast"}
    text = (tmp_path / "data/wiki" / rel).read_text()
    assert text == (
        "---\ndate: 2026-09-04\npeople:\n- alex\n- mia\nsource: agent\n---\n\n"
        "# Oatmeal for breakfast\n\nAlex had porridge.\n"
    )


def test_saving_a_day_page_keeps_it_a_day_page(client, tmp_path):
    rel = "journal/2026/09/04/2026-09-04.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    client.post(
        "/api/v1/save",
        json={"path": rel, "csrf": token, "people": "alex", "body": "Only the flights.\r\n"},
        auth=ALEX,
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
        "/api/v1/save",
        json={"path": rel, "csrf": token, "people": "Not A Person", "body": "x"},
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
        response = client.post(
            "/api/v1/save", json={"path": rel, "csrf": token, "body": "x"}, auth=ALEX
        )
        assert response.status_code in (403, 404), rel
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    response = client.post(
        "/api/v1/save", json={"path": rel, "csrf": "bad", "body": "x"}, auth=ALEX
    )
    assert response.status_code == 403


def test_save_with_a_bad_origin_is_403(client):
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    response = client.post(
        "/api/v1/save",
        json={"path": rel, "csrf": token, "body": "x"},
        headers={"Origin": "http://evil.example"},
        auth=ALEX,
    )
    assert response.status_code == 403


def test_edit_of_a_missing_entry_is_404(client):
    params = {"path": "journal/2026/09/04/nope.md"}
    assert client.get("/api/v1/edit", params=params, auth=ALEX).status_code == 404


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_save_commits_the_edit(client, tmp_path):
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    rel = "journal/2026/09/04/oatmeal-breakfast.md"
    token = viewer._csrf_token("alex", "wiki/" + rel)
    client.post(
        "/api/v1/save",
        json={"path": rel, "csrf": token, "people": "alex", "body": "Porridge."},
        auth=ALEX,
    )
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert f"viewer: edit {rel}" in log

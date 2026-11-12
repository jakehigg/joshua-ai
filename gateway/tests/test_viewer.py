"""Offline tests for the read-only viewer over a temp data volume.

Each test builds a fresh viewer app from a temp ``joshua.yaml`` and a temp data
root, with the viewer passwords set in the environment. The Starlette
``TestClient`` drives the app in-process.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import bcrypt
import pytest
from joshua_gateway import viewer, viewer_api
from joshua_shared import config
from starlette.routing import Route
from starlette.testclient import TestClient

ALEX_PW = "alex-secret"
MIA_PW = "mia-secret"

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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    _write(data / "wiki/pizza.md", "# Pizza\n\nDough and sauce.\n")
    _write(data / "wiki/raw.md", "# Raw\n\n<script>alert(1)</script>\n<b>bold</b>\n")
    _write(
        data / "wiki/dated.md",
        "---\ndate: 2026-08-20\nauthor: alex\n---\n\n# Dated\n\nA page with frontmatter.\n",
    )
    _write(data / "wiki/people/alex.md", "# Alex\n\n## About\n\nA member.\n")
    _write(data / "wiki/people/mia.md", "# Mia\n\n## About\n\nA guest.\n")
    _write(data / "wiki/people/everyone.md", "# Home\n\nShared facts.\n")
    # A one-pixel PNG for the inline-image check.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
    )
    (data / "people/alex/attachments/2026/08").mkdir(parents=True, exist_ok=True)
    (data / "people/alex/attachments/2026/08/pic.png").write_bytes(png)
    (data / "shared/attachments").mkdir(parents=True, exist_ok=True)
    (data / "shared/attachments/team.png").write_bytes(png)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    data.mkdir()
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)

    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX_PW)
    monkeypatch.setenv("VIEWER_PW_MIA", MIA_PW)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return TestClient(viewer.build_app())


MIA = ("mia", MIA_PW)

# The wiki, the shared files, the own attachments, the journal and the search
# are the React app, which reads these routes.
PAGE_JSON = "/api/v1/wiki?path=wiki/pizza.md"
SHARED_PNG = "/api/v1/files/shared/attachments/team.png"
FEED_JSON = "/api/v1/journal"
SEARCH_JSON = "/api/v1/search?q=Dough"


# -- auth -------------------------------------------------------------------


def test_every_route_needs_auth(client):
    for path in (PAGE_JSON, SHARED_PNG, FEED_JSON, SEARCH_JSON):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_good_credential_reads_a_page(client):
    response = client.get(PAGE_JSON, auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert "Dough and sauce" in response.json()["markdown"]


def test_bad_password_is_401(client):
    assert client.get(FEED_JSON, auth=("alex", "wrong")).status_code == 401
    assert client.get(FEED_JSON, auth=("nobody", "x")).status_code == 401


def test_bcrypt_hash_reference(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _tree(data)
    hashed = bcrypt.hashpw(b"hunter2", bcrypt.gensalt(rounds=4)).decode()
    cfg = CONFIG.replace("alex: ${VIEWER_PW_ALEX:-}", f"alex: {hashed}")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(cfg)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_MIA", MIA_PW)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    client = TestClient(viewer.build_app())
    assert client.get(FEED_JSON, auth=("alex", "hunter2")).status_code == 200
    assert client.get(FEED_JSON, auth=("alex", "nope")).status_code == 401


# -- access -----------------------------------------------------------------


def test_member_and_guest_both_read_the_wiki(client):
    for creds in (("alex", ALEX_PW), MIA):
        assert client.get(PAGE_JSON, auth=creds).status_code == 200


def test_the_page_of_the_person_reads_its_json(client):
    """``/me`` is a React page now: its profile link, its attachments and its
    wiki tree come from the JSON routes."""
    me = client.get("/api/v1/me", auth=("alex", ALEX_PW)).json()
    assert me["profile_url"] == "/wiki/people/alex.md"
    assert {"path": "2026/08/pic.png", "url": "/attachments/2026/08/pic.png"} in me["attachments"]
    tree = client.get("/api/v1/wiki/tree", auth=("alex", ALEX_PW)).json()
    assert "/wiki/pizza.md" in {child["url"] for child in tree["children"]}


def test_home_has_no_blog_route(client):
    """The journal and the profile file left ``people/``; only the wiki route
    to a person's profile page remains."""
    assert client.get("/blog/", auth=("alex", ALEX_PW)).status_code == 404
    assert client.get("/profile", auth=("alex", ALEX_PW)).status_code == 404


def test_shared_profile_lives_in_the_wiki(client):
    body = client.get("/api/v1/wiki?path=wiki/people/everyone.md", auth=("alex", ALEX_PW)).json()
    assert "Shared facts" in body["markdown"]


def test_shared_attachment_reads(client):
    response = client.get(SHARED_PNG, auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_a_guest_reads_another_persons_profile_in_the_wiki(client):
    """Profiles live in the shared wiki now: everyone reads every page."""
    response = client.get("/api/v1/wiki?path=wiki/people/alex.md", auth=MIA)
    assert response.status_code == 200
    assert "A member" in response.json()["markdown"]


# -- dot entries --------------------------------------------------------------


def test_dot_entries_are_hidden_from_home_and_search(client, tmp_path):
    """A wiki frontend's own state never shows up as content, at any depth."""
    data = tmp_path / "data"
    _write(data / "wiki/.git/config", "not markdown\n")
    _write(data / "wiki/.obsidian/workspace.md", "# Workspace\n\nA note tool's own state.\n")
    tree = client.get("/api/v1/wiki/tree", auth=("alex", ALEX_PW)).text
    assert "/wiki/.git" not in tree
    assert "/wiki/.obsidian" not in tree
    hits = client.get("/api/v1/search", params={"q": "Workspace"}, auth=("alex", ALEX_PW)).text
    assert "/wiki/.obsidian" not in hits


def test_reading_a_dot_path_directly_is_404(client, tmp_path):
    data = tmp_path / "data"
    _write(data / "wiki/.git/config", "secret\n")
    response = client.get("/api/v1/files/wiki/.git/config", auth=("alex", ALEX_PW))
    assert response.status_code == 404


# -- path safety ------------------------------------------------------------


def test_dotdot_escape_is_404(client):
    response = client.get("/api/v1/files/wiki/%2e%2e/%2e%2e/etc/hostname", auth=("alex", ALEX_PW))
    assert response.status_code == 404


def test_symlink_out_of_root_is_404(client, tmp_path):
    link = tmp_path / "data" / "wiki" / "evil.md"
    link.symlink_to("/etc/hostname")
    response = client.get("/api/v1/wiki?path=wiki/evil.md", auth=("alex", ALEX_PW))
    assert response.status_code == 404


# -- rendering --------------------------------------------------------------


def test_the_html_in_a_page_stays_text(client):
    """The JSON route sends the body as Markdown, so the HTML in a page is
    text. The app renders it with no raw-HTML plugin and sanitizes the
    result, and no page of the viewer ever renders that HTML as markup."""
    body = client.get("/api/v1/wiki?path=wiki/raw.md", auth=("alex", ALEX_PW))
    assert body.headers["content-type"].startswith("application/json")
    assert "<script>alert(1)</script>" in body.json()["markdown"]


def test_frontmatter_comes_as_pairs(client):
    body = client.get("/api/v1/wiki?path=wiki/dated.md", auth=("alex", ALEX_PW)).json()
    assert body["frontmatter"] == {"date": "2026-08-20", "author": "alex"}


def test_image_attachment_renders_inline(client):
    response = client.get("/api/v1/files/attachments/2026/08/pic.png", auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_security_headers_on_every_response(client):
    response = client.get("/assignments", auth=("alex", ALEX_PW))
    # The policy allows the bundle of the React app, from this origin, and
    # nothing else. No inline script, so no hash.
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; img-src 'self'; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; "
        "frame-src 'self'; frame-ancestors 'self'"
    )
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_only_the_same_origin_may_frame_the_viewer(client):
    # The chat frames a document page of the viewer; no other origin can.
    policy = client.get("/assignments", auth=("alex", ALEX_PW)).headers["content-security-policy"]
    assert "frame-ancestors 'self'" in policy
    assert "frame-ancestors *" not in policy
    assert "autrui.net" not in policy


# -- the route table ---------------------------------------------------------


def test_the_route_table_is_the_probes_the_api_and_the_two_documents(client):
    """The two probes, every JSON route of ``viewer_api``, and the two routes
    of a document the chat calls. The mount of the React app is last, and an
    image with no build has none."""
    routes = [route for route in viewer.build_app().routes if isinstance(route, Route)]
    assert [route.path for route in routes[:2]] == ["/healthz", "/readyz"]
    api = [route.path for route in viewer_api.routes()]
    assert [route.path for route in routes[2 : 2 + len(api)]] == api
    assert [route.path for route in routes[2 + len(api) :]] == [
        "/documents/{owner}/{slug}.json",
        "/documents/{owner}/{slug}/artifact",
    ]


def test_the_viewer_serves_no_stylesheet_and_no_image_of_its_own(client):
    """Every stylesheet and every image is a file of the React app now."""
    for path in ("/style.css", "/ui/joshua-ui.css", "/ui/logo-64.png"):
        assert client.get(path, auth=("alex", ALEX_PW)).status_code == 404, path


# -- search -----------------------------------------------------------------


def test_search_finds_a_substring(client):
    """The search is the React page, which reads this route
    (``test_viewer_api_search``)."""
    body = client.get(SEARCH_JSON, auth=("alex", ALEX_PW)).json()
    assert "/wiki/pizza.md" in [hit["url"] for hit in body["words"]]


# -- delete -----------------------------------------------------------------


def test_guest_gets_no_delete_token(client):
    """The page of a guest carries no token, so it draws no delete control.
    The delete itself is ``POST /api/v1/delete`` (``test_viewer_api_edit``)."""
    guest = client.get(PAGE_JSON, auth=MIA).json()
    assert guest["can_edit"] is False and guest["delete_csrf"] == ""
    member = client.get(PAGE_JSON, auth=("alex", ALEX_PW)).json()
    assert member["can_edit"] is True and member["delete_csrf"] != ""


# -- probes -----------------------------------------------------------------


def test_healthz_needs_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_readyz_needs_no_auth_and_leaks_no_name(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["users"] == 2
    # No person name or password appears.
    assert "alex" not in response.text
    assert ALEX_PW not in response.text


# -- where a password comes from -----------------------------------------------

# `viewer.users` with empty values: the compose file names no person, so every
# password arrives through one environment variable.
CONFIG_NO_REFERENCES = textwrap.dedent("""\
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
        alex: ""
        mia: ""
    """)


@pytest.fixture
def env_client(monkeypatch, tmp_path):
    """A viewer whose passwords can only come from the environment."""

    def build(*, passwords: str = "", pw_vars: dict[str, str] | None = None) -> TestClient:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        _tree(data)
        cfg_path = tmp_path / "joshua.yaml"
        cfg_path.write_text(CONFIG_NO_REFERENCES)

        monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
        monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
        monkeypatch.delenv("VIEWER_PW_ALEX", raising=False)
        monkeypatch.delenv("VIEWER_PW_MIA", raising=False)
        monkeypatch.setenv(viewer.VIEWER_PASSWORDS_ENV, passwords)
        for name, value in (pw_vars or {}).items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setattr(config, "_cache_path", None)
        monkeypatch.setattr(config, "_cache_people_file_mtime", None)
        return TestClient(viewer.build_app())

    return build


def test_one_variable_carries_every_password(env_client):
    """The compose file passed VIEWER_PW_ALEX, which named the example person."""
    client = env_client(passwords=f"alex={ALEX_PW},mia={MIA_PW}")
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 200
    assert client.get(PAGE_JSON, auth=("mia", MIA_PW)).status_code == 200


def test_a_person_whose_id_is_not_the_example_id_signs_in(env_client):
    """No tracked file names a person, so a real install needs no edit to one."""
    client = env_client(passwords=f"mia={MIA_PW}")
    assert client.get(PAGE_JSON, auth=("mia", MIA_PW)).status_code == 200


def test_a_bcrypt_hash_in_the_variable_works(env_client):
    hashed = bcrypt.hashpw(ALEX_PW.encode(), bcrypt.gensalt()).decode()
    client = env_client(passwords=f"alex={hashed}")
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 200
    assert client.get(PAGE_JSON, auth=("alex", "wrong")).status_code == 401


def test_a_person_not_in_viewer_users_never_signs_in(env_client):
    """`viewer.users` is the authorization. The environment is only the secret."""
    client = env_client(passwords=f"nobody={ALEX_PW}")
    assert client.get(PAGE_JSON, auth=("nobody", ALEX_PW)).status_code == 401


def test_an_empty_variable_signs_nobody_in(env_client):
    client = env_client(passwords="")
    assert client.get(PAGE_JSON, auth=("alex", "")).status_code == 401
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 401


def test_a_malformed_pair_is_ignored(env_client):
    client = env_client(passwords=f"nonsense,,alex={ALEX_PW},mia=")
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 200
    assert client.get(PAGE_JSON, auth=("mia", "")).status_code == 401


def test_the_per_person_variable_still_works(env_client):
    """An installation that injects VIEWER_PW_<ID> its own way keeps working."""
    client = env_client(pw_vars={"VIEWER_PW_ALEX": ALEX_PW})
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 200


def test_the_config_entry_wins_over_the_environment(client):
    """The first fixture still resolves through ${VIEWER_PW_<ID>} in joshua.yaml."""
    assert client.get(PAGE_JSON, auth=("alex", ALEX_PW)).status_code == 200


def test_a_page_shows_a_wiki_attachment(client, tmp_path):
    """A picture a page uses is served, and the link is corrected for this viewer."""
    data = tmp_path / "data"
    png = (data / "people/alex/attachments/2026/08/pic.png").read_bytes()
    folder = data / "wiki" / "attachments" / "2026" / "09"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plant.png").write_bytes(png)
    _write(data / "wiki/plants.md", "# Plants\n\n![plant](/attachments/2026/09/plant.png)\n")

    page = client.get("/api/v1/wiki?path=wiki/plants.md", auth=("alex", ALEX_PW))
    assert page.status_code == 200
    # The stored link is never changed; the app points it at the file route.
    assert "![plant](/attachments/2026/09/plant.png)" in page.json()["markdown"]

    image = client.get("/api/v1/files/wiki/attachments/2026/09/plant.png", auth=("alex", ALEX_PW))
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


def test_one_person_still_reaches_their_own_files_at_attachments(client):
    """The rewrite changes a page's links only, never the viewer's own root."""
    response = client.get("/api/v1/files/attachments/2026/08/pic.png", auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

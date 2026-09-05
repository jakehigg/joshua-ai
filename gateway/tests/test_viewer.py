"""Offline tests for the read-only viewer over a temp data volume.

Each test builds a fresh viewer app from a temp ``joshua.yaml`` and a temp data
root, with the viewer passwords set in the environment. The Starlette
``TestClient`` drives the app in-process.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import bcrypt
import pytest
from joshua_gateway import viewer
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

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


# -- auth -------------------------------------------------------------------


def test_every_route_needs_auth(client):
    for path in ("/", "/wiki/pizza.md", "/shared/attachments/team.png", "/search"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_good_credential_reads_a_page(client):
    response = client.get("/wiki/pizza.md", auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert "Dough and sauce" in response.text


def test_bad_password_is_401(client):
    assert client.get("/", auth=("alex", "wrong")).status_code == 401
    assert client.get("/", auth=("nobody", "x")).status_code == 401


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
    assert client.get("/", auth=("alex", "hunter2")).status_code == 200
    assert client.get("/", auth=("alex", "nope")).status_code == 401


# -- access -----------------------------------------------------------------


def test_member_and_guest_both_read_the_wiki(client):
    for creds in (("alex", ALEX_PW), MIA):
        assert client.get("/wiki/pizza.md", auth=creds).status_code == 200


def test_home_lists_profile_link_wiki_and_attachments(client):
    text = client.get("/", auth=("alex", ALEX_PW)).text
    assert "/wiki/people/alex.md" in text
    assert "/wiki/pizza.md" in text
    assert "/attachments/2026/08/pic.png" in text


def test_home_has_no_blog_route(client):
    """The journal and the profile file left ``people/``; only the wiki route
    to a person's profile page remains."""
    assert client.get("/blog/", auth=("alex", ALEX_PW)).status_code == 404
    assert client.get("/profile", auth=("alex", ALEX_PW)).status_code == 404


def test_shared_profile_lives_in_the_wiki(client):
    text = client.get("/wiki/people/everyone.md", auth=("alex", ALEX_PW)).text
    assert "Shared facts" in text


def test_shared_attachment_reads(client):
    response = client.get("/shared/attachments/team.png", auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_a_guest_reads_another_persons_profile_in_the_wiki(client):
    """Profiles live in the shared wiki now: everyone reads every page."""
    response = client.get("/wiki/people/alex.md", auth=MIA)
    assert response.status_code == 200
    assert "A member" in response.text


# -- dot entries --------------------------------------------------------------


def test_dot_entries_are_hidden_from_home_and_search(client, tmp_path):
    """A wiki frontend's own state never shows up as content, at any depth."""
    data = tmp_path / "data"
    _write(data / "wiki/.git/config", "not markdown\n")
    _write(data / "wiki/.obsidian/workspace.md", "# Workspace\n\nA note tool's own state.\n")
    home = client.get("/", auth=("alex", ALEX_PW)).text
    assert "/wiki/.git" not in home
    assert "/wiki/.obsidian" not in home
    hits = client.get("/search", params={"q": "Workspace"}, auth=("alex", ALEX_PW)).text
    assert "/wiki/.obsidian" not in hits


def test_reading_a_dot_path_directly_is_404(client, tmp_path):
    data = tmp_path / "data"
    _write(data / "wiki/.git/config", "secret\n")
    response = client.get("/wiki/.git/config", auth=("alex", ALEX_PW))
    assert response.status_code == 404


# -- path safety ------------------------------------------------------------


def test_dotdot_escape_is_404(client):
    response = client.get("/wiki/%2e%2e/%2e%2e/etc/hostname", auth=("alex", ALEX_PW))
    assert response.status_code == 404


def test_symlink_out_of_root_is_404(client, tmp_path):
    link = tmp_path / "data" / "wiki" / "evil.md"
    link.symlink_to("/etc/hostname")
    response = client.get("/wiki/evil.md", auth=("alex", ALEX_PW))
    assert response.status_code == 404


# -- rendering --------------------------------------------------------------


def test_raw_html_is_escaped(client):
    text = client.get("/wiki/raw.md", auth=("alex", ALEX_PW)).text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_frontmatter_shows_a_header(client):
    text = client.get("/wiki/dated.md", auth=("alex", ALEX_PW)).text
    assert "frontmatter" in text
    assert "2026-08-20" in text


def test_image_attachment_renders_inline(client):
    response = client.get("/attachments/2026/08/pic.png", auth=("alex", ALEX_PW))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_security_headers_on_every_response(client):
    response = client.get("/wiki/pizza.md", auth=("alex", ALEX_PW))
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; img-src 'self'; style-src 'self'"
    )
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


# -- search -----------------------------------------------------------------


def test_search_finds_a_substring(client):
    text = client.get("/search", params={"q": "Dough"}, auth=("alex", ALEX_PW)).text
    assert "/wiki/pizza.md" in text


# -- delete -----------------------------------------------------------------


def test_member_deletes_a_wiki_page(client, tmp_path):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("alex", path)
    response = client.post(
        "/delete",
        data={"path": path, "csrf": token},
        auth=("alex", ALEX_PW),
        follow_redirects=False,
    )
    assert response.status_code == 303
    original = tmp_path / "data" / "wiki" / "pizza.md"
    assert not original.exists()
    trash = list((tmp_path / "data" / "wiki" / ".trash").rglob("pizza.md"))
    assert len(trash) == 1
    assert trash[0].read_text().startswith("# Pizza")


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_delete_commits_the_removal(client, tmp_path):
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")  # tracks pizza.md first
    path = "wiki/pizza.md"
    token = viewer._csrf_token("alex", path)
    response = client.post(
        "/delete",
        data={"path": path, "csrf": token},
        auth=("alex", ALEX_PW),
        follow_redirects=False,
    )
    assert response.status_code == 303
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: trash pizza.md" in log


def test_guest_cannot_delete(client):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("mia", path)
    response = client.post("/delete", data={"path": path, "csrf": token}, auth=MIA)
    assert response.status_code == 403


def test_guest_sees_no_delete_control(client):
    assert "move this page to trash" not in client.get("/wiki/pizza.md", auth=MIA).text
    assert "move this page to trash" in client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).text


def test_wrong_csrf_is_403(client):
    response = client.post(
        "/delete", data={"path": "wiki/pizza.md", "csrf": "bad"}, auth=("alex", ALEX_PW)
    )
    assert response.status_code == 403


def test_missing_csrf_is_403(client):
    response = client.post("/delete", data={"path": "wiki/pizza.md"}, auth=("alex", ALEX_PW))
    assert response.status_code == 403


def test_delete_outside_wiki_is_refused(client):
    # An absolute path escapes every root; an attachment path is not under wiki.
    for path in ("/etc/passwd", "people/alex/attachments/2026/08/pic.png"):
        token = viewer._csrf_token("alex", path)
        response = client.post(
            "/delete", data={"path": path, "csrf": token}, auth=("alex", ALEX_PW)
        )
        assert response.status_code in (403, 404), path


def test_bad_origin_is_403(client):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("alex", path)
    response = client.post(
        "/delete",
        data={"path": path, "csrf": token},
        headers={"Origin": "http://evil.example"},
        auth=("alex", ALEX_PW),
    )
    assert response.status_code == 403


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
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 200
    assert client.get("/wiki/pizza.md", auth=("mia", MIA_PW)).status_code == 200


def test_a_person_whose_id_is_not_the_example_id_signs_in(env_client):
    """No tracked file names a person, so a real install needs no edit to one."""
    client = env_client(passwords=f"mia={MIA_PW}")
    assert client.get("/wiki/pizza.md", auth=("mia", MIA_PW)).status_code == 200


def test_a_bcrypt_hash_in_the_variable_works(env_client):
    hashed = bcrypt.hashpw(ALEX_PW.encode(), bcrypt.gensalt()).decode()
    client = env_client(passwords=f"alex={hashed}")
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 200
    assert client.get("/wiki/pizza.md", auth=("alex", "wrong")).status_code == 401


def test_a_person_not_in_viewer_users_never_signs_in(env_client):
    """`viewer.users` is the authorization. The environment is only the secret."""
    client = env_client(passwords=f"nobody={ALEX_PW}")
    assert client.get("/wiki/pizza.md", auth=("nobody", ALEX_PW)).status_code == 401


def test_an_empty_variable_signs_nobody_in(env_client):
    client = env_client(passwords="")
    assert client.get("/wiki/pizza.md", auth=("alex", "")).status_code == 401
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 401


def test_a_malformed_pair_is_ignored(env_client):
    client = env_client(passwords=f"nonsense,,alex={ALEX_PW},mia=")
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 200
    assert client.get("/wiki/pizza.md", auth=("mia", "")).status_code == 401


def test_the_per_person_variable_still_works(env_client):
    """An installation that injects VIEWER_PW_<ID> its own way keeps working."""
    client = env_client(pw_vars={"VIEWER_PW_ALEX": ALEX_PW})
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 200


def test_the_config_entry_wins_over_the_environment(client):
    """The first fixture still resolves through ${VIEWER_PW_<ID>} in joshua.yaml."""
    assert client.get("/wiki/pizza.md", auth=("alex", ALEX_PW)).status_code == 200

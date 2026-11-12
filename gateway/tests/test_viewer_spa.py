"""Offline tests for the mount of the React app: the fallback to
``index.html``, the paths that keep their 404, the cache headers, and the
sign-in in front of the app."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_spa
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")

INDEX = "<!doctype html><html><body><div id=root></div></body></html>"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)

# ``dedent`` already took the indent off, so the line of the viewer block
# carries two spaces and the key goes in beside it.
CONFIG_ANON = CONFIG.replace("  enabled: true\n", "  enabled: true\n  anonymous: true\n")


def _env(monkeypatch, tmp_path: Path, text: str) -> None:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    (data / "wiki/Home.md").write_text("# Home\n\nThe front door.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(text)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])


def _build(monkeypatch, tmp_path: Path) -> Path:
    """Write a build of the frontend the way the image does."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX)
    (static / "assets/main-abc123.js").write_text("console.log(1)\n")
    (static / "favicon-32.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(viewer_spa, "static_dir", lambda: static)
    return static


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    _env(monkeypatch, tmp_path, CONFIG)
    _build(monkeypatch, tmp_path)
    return TestClient(viewer.build_app())


@pytest.fixture
def bare_client(monkeypatch, tmp_path) -> TestClient:
    """The viewer with no build, as a checkout and the test suite have it."""
    _env(monkeypatch, tmp_path, CONFIG)
    monkeypatch.setattr(viewer_spa, "static_dir", lambda: tmp_path / "nothing")
    return TestClient(viewer.build_app())


def test_an_unknown_app_path_gets_the_index(client) -> None:
    response = client.get("/me", auth=ALEX)
    assert response.status_code == 200
    assert "id=root" in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_a_json_path_keeps_its_404(client) -> None:
    assert client.get("/api/v1/nope", auth=ALEX).status_code == 404
    assert client.get("/api/nope", auth=ALEX).status_code == 404


def test_a_file_path_keeps_its_404(client) -> None:
    assert client.get("/x.png", auth=ALEX).status_code == 404
    assert client.get("/assets/gone-123.js", auth=ALEX).status_code == 404


def test_the_chat_is_a_screen_of_the_app(client) -> None:
    """``/chat`` and ``/chat/<person>`` are routes of the router."""
    for path in ("/chat", "/chat/alex"):
        response = client.get(path, auth=ALEX)
        assert response.status_code == 200, path
        assert response.text == INDEX
    # The JSON routes of the chat stay out of the app.
    assert client.get("/api/v1/chat/nobody/nope", auth=ALEX).status_code == 404


def test_an_asset_is_immutable(client) -> None:
    response = client.get("/assets/main-abc123.js", auth=ALEX)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_app_needs_the_same_sign_in_as_a_page(client) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/me", auth=("alex", "wrong")).status_code == 401


def test_with_anonymous_the_app_opens(monkeypatch, tmp_path) -> None:
    _env(monkeypatch, tmp_path, CONFIG_ANON)
    _build(monkeypatch, tmp_path)
    assert TestClient(viewer.build_app()).get("/me").status_code == 200


def test_every_explicit_route_still_wins(client) -> None:
    assert client.get("/assignments", auth=ALEX).status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/api/v1/me", auth=ALEX).json()["person"]["id"] == "alex"


def test_the_two_document_routes_of_the_chat_are_not_the_app(client) -> None:
    """``<slug>.json`` and ``<slug>/artifact`` answer from the server, so a
    document that is not there is 404 and never ``index.html``."""
    for path in ("/documents/alex/party.json", "/documents/alex/party/artifact"):
        response = client.get(path, auth=ALEX)
        assert response.status_code == 404, path
        assert "id=root" not in response.text, path


def test_the_content_of_the_viewer_is_the_app(client) -> None:
    """The wiki, the shared files, the own attachments, the journal, the
    search, the usage, the links, the documents and the editor are the app,
    which reads them from ``GET /api/v1/wiki``, ``GET /api/v1/journal``,
    ``GET /api/v1/search``, ``GET /api/v1/usage``, ``GET /api/v1/links``,
    ``GET /api/v1/documents`` and ``GET /api/v1/edit``."""
    for path in (
        "/",
        "/wiki/",
        "/wiki/Home.md",
        "/attachments/2026/09/a.jpg",
        "/journal",
        "/journal/2026/09/04",
        "/search",
        "/usage",
        "/links",
        "/documents",
        "/documents/alex/party",
        "/edit/Home.md",
        "/new",
    ):
        response = client.get(path, auth=ALEX)
        assert response.status_code == 200, path
        assert "id=root" in response.text, path


def test_with_no_build_there_is_no_mount(bare_client) -> None:
    assert viewer_spa.mount() is None
    assert bare_client.get("/me", auth=ALEX).status_code == 404
    assert bare_client.get("/wiki/", auth=ALEX).status_code == 404


def test_the_static_folder_is_in_the_package() -> None:
    assert viewer_spa.static_dir().name == "static"
    assert viewer_spa.static_dir().parent.name == "joshua_gateway"


def test_which_path_falls_back_to_the_app() -> None:
    assert viewer_spa.falls_back("me") is True
    assert viewer_spa.falls_back("documents/alex/plan") is True
    # The content of the viewer, where a name holds a dot.
    assert viewer_spa.falls_back("documents/alex/plan.v2") is True
    assert viewer_spa.falls_back("wiki/recipes/pizza.md") is True
    assert viewer_spa.falls_back("shared/attachments/team.png") is True
    assert viewer_spa.falls_back("attachments/2026/09/a.jpg") is True
    assert viewer_spa.falls_back("edit/recipes/pizza.md") is True
    assert viewer_spa.falls_back("api/v1/session") is False
    assert viewer_spa.falls_back("chat") is True
    assert viewer_spa.falls_back("chat/alex") is True
    assert viewer_spa.falls_back("api/v1/chat/alex/turns") is False
    assert viewer_spa.falls_back("logo-64.png") is False

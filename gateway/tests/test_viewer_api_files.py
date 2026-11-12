"""Offline tests for ``GET /api/v1/files/{root}/{path}``: the bytes of one
file of the viewer.

An image answers with its own media type, so a browser shows it inline.
Anything else answers as an attachment with the name of the file. Only the
three display roots of the viewer are reachable, and every path goes through
the resolver of the files MCP.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_gateway.viewer_api import files as files_api
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

# A one-pixel PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tree(data: Path) -> None:
    _write_bytes(data / "wiki" / "attachments" / "2026" / "09" / "plant.png", PNG)
    _write_bytes(data / "wiki" / "notes" / 'a "quoted" name.txt', b"text\n")
    _write_bytes(data / "wiki" / "pizza.md", b"# Pizza\n")
    _write_bytes(data / "shared" / "attachments" / "team.png", PNG)
    own = data / "people" / "alex" / "attachments" / "2026" / "08"
    _write_bytes(own / "receipt.txt", b"3.50\n")


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return TestClient(viewer.build_app())


def test_an_image_shows_inline_with_its_own_type(client) -> None:
    response = client.get("/api/v1/files/wiki/attachments/2026/09/plant.png", auth=ALEX)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "content-disposition" not in response.headers
    assert response.content == PNG


def test_a_shared_image_and_an_own_attachment_both_answer(client) -> None:
    assert client.get("/api/v1/files/shared/attachments/team.png", auth=ALEX).content == PNG
    own = client.get("/api/v1/files/attachments/2026/08/receipt.txt", auth=ALEX)
    assert own.status_code == 200 and own.content == b"3.50\n"


def test_another_file_downloads_with_its_name(client) -> None:
    response = client.get("/api/v1/files/attachments/2026/08/receipt.txt", auth=ALEX)
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="receipt.txt"'


def test_a_quote_in_a_name_never_reaches_the_header(client) -> None:
    response = client.get('/api/v1/files/wiki/notes/a "quoted" name.txt', auth=ALEX)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="a quoted name.txt"'


def test_a_name_with_a_newline_or_a_backslash_is_cleaned() -> None:
    assert files_api._safe_filename('a"b\\c\r\nd.txt') == "abcd.txt"


def test_a_page_asked_for_here_downloads(client) -> None:
    """The file route delivers bytes. A page of the wiki is read as a page
    through ``GET /api/v1/wiki``."""
    response = client.get("/api/v1/files/wiki/pizza.md", auth=ALEX)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="pizza.md"'


def test_an_unknown_root_is_404(client) -> None:
    for path in (
        "/api/v1/files/people/alex/attachments/2026/08/receipt.txt",
        "/api/v1/files/secrets/key.txt",
        "/api/v1/files/etc/hostname",
    ):
        response = client.get(path, auth=ALEX)
        assert response.status_code == 404, path
        assert response.json() == {"error": "not found"}


def test_a_missing_file_and_a_folder_are_404(client) -> None:
    for path in ("/api/v1/files/wiki/nope.png", "/api/v1/files/wiki/attachments"):
        assert client.get(path, auth=ALEX).status_code == 404, path


def test_a_path_that_escapes_its_root_is_404(client, tmp_path) -> None:
    (tmp_path / "data" / "wiki" / "evil.png").symlink_to("/etc/hostname")
    for path in (
        "/api/v1/files/wiki/%2e%2e/%2e%2e/etc/hostname",
        "/api/v1/files/wiki/.git/config",
        "/api/v1/files/wiki/evil.png",
    ):
        assert client.get(path, auth=ALEX).status_code == 404, path


def test_the_file_route_needs_a_sign_in(client) -> None:
    path = "/api/v1/files/wiki/attachments/2026/09/plant.png"
    unsigned = client.get(path)
    assert unsigned.status_code == 401
    assert unsigned.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get(path, auth=MIA).status_code == 200  # a guest reads


def test_the_address_of_a_file_is_the_route() -> None:
    url = files_api.file_url("wiki", "attachments/2026/09/a b.png")
    assert url == "/api/v1/files/wiki/attachments/2026/09/a%20b.png"

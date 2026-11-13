"""`/people/{id}/profile`: who may call, what is read, and that a write keeps
the rest of the person. A real config and people file stand in for the roster."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from joshua_core.profile_api import build_profile_router
from joshua_shared import config as config_module
from starlette.testclient import TestClient

VIEWER = "viewer-token"
MCP = "mcp-token"

CONFIG = (
    "name: Test House\ntimezone: UTC\npeople:\n"
    "  - id: robin\n    name: Robin\n    role: guest\n    journal: ask\n    prompt: {snippet}\n"
    '    handles:\n      telegram: "112233"\n      imessage:\n        - "+15551230001"\n'
    '        - "robin@example.com"\n'
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "joshua.yaml"
    snippet = tmp_path / "robin-config.md"
    snippet.write_text("Be brief.\n")
    cfg.write_text(CONFIG.format(snippet=snippet))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("JOSHUA_CONFIG", str(cfg))
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_MCP", MCP)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    monkeypatch.setattr(config_module, "_cache", None)
    monkeypatch.setattr(config_module, "_cache_path", None)
    monkeypatch.setattr(config_module, "_cache_people_file_mtime", None)
    return data


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_profile_router())
    return TestClient(app)


def _h(token: str | None = VIEWER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _file(data_dir: Path) -> list[dict]:
    return config_module.read_people_file(data_dir / "people.yaml")


def test_read_a_configured_profile(client) -> None:
    response = client.get("/people/robin/profile", headers=_h())
    assert response.status_code == 200
    assert response.json() == {"prompt": "Be brief.", "journal": "ask", "in_config": True}


def test_read_an_unknown_person_is_404(client) -> None:
    assert client.get("/people/nobody/profile", headers=_h()).status_code == 404
    assert (
        client.post("/people/nobody/profile", headers=_h(), json={"journal": "off"}).status_code
        == 404
    )


def test_write_prompt_and_journal_keeps_the_rest(client, data_dir) -> None:
    response = client.post(
        "/people/robin/profile", headers=_h(), json={"prompt": "  Call me Rob.  ", "journal": "off"}
    )
    assert response.status_code == 200
    assert response.json() == {"prompt": "Call me Rob.", "journal": "off", "in_config": True}
    own = data_dir / "people" / "robin" / "prompt.md"
    # The file holds the text, the overlay holds the path, and only those two keys.
    assert own.read_text().strip() == "Call me Rob."
    assert _file(data_dir) == [{"id": "robin", "prompt": str(own), "journal": "off"}]
    # The config prompt file is never written.
    assert (data_dir.parent / "robin-config.md").read_text() == "Be brief.\n"
    # The loader reads the merged person back whole.
    robin = config_module.reload(Path(config_module._resolve_path(None))).person("robin")
    assert robin.prompt == str(own) and robin.journal == "off"
    assert robin.name == "Robin"
    assert robin.handles["imessage"] == ["+15551230001", "robin@example.com"]
    assert robin.role == "guest"


def test_write_one_field_leaves_the_other(client, data_dir) -> None:
    response = client.post("/people/robin/profile", headers=_h(), json={"journal": "auto"})
    assert response.json()["prompt"] == "Be brief."
    assert response.json()["journal"] == "auto"
    # A journal-only write keeps the configured prompt path.
    assert _file(data_dir)[0]["prompt"].endswith("robin-config.md")
    response = client.post("/people/robin/profile", headers=_h(), json={"prompt": "New."})
    assert response.json() == {"prompt": "New.", "journal": "auto", "in_config": True}


@pytest.mark.parametrize("value", ["", None, "   "])
def test_an_empty_prompt_clears_it_even_after_an_earlier_write(client, data_dir, value) -> None:
    client.post("/people/robin/profile", headers=_h(), json={"prompt": "First."})
    response = client.post("/people/robin/profile", headers=_h(), json={"prompt": value})
    assert response.json()["prompt"] is None
    assert _file(data_dir)[0]["prompt"] == ""
    assert not (data_dir / "people" / "robin" / "prompt.md").exists()
    # The empty path overrides the config prompt path, after a reload too.
    robin = config_module.reload(Path(config_module._resolve_path(None))).person("robin")
    assert robin.prompt in ("", None)
    assert client.get("/people/robin/profile", headers=_h()).json()["prompt"] is None


def test_a_person_only_in_the_people_file(client, data_dir) -> None:
    from joshua_core.store import people_file

    people_file.write_people_file(
        data_dir / "people.yaml",
        [{"id": "sam", "name": "Sam", "role": "member", "handles": {"telegram": "9"}}],
    )
    assert client.get("/people/sam/profile", headers=_h()).json()["in_config"] is False
    response = client.post("/people/sam/profile", headers=_h(), json={"prompt": "Hi"})
    assert response.json() == {"prompt": "Hi", "journal": "auto", "in_config": False}
    sam = next(e for e in _file(data_dir) if e["id"] == "sam")
    assert sam["handles"] == {"telegram": "9"} and sam["name"] == "Sam"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"journal": "sometimes"},
        {"journal": None},
        {"prompt": 5},
        {"prompt": ["a"]},
        {"prompt": "x" * 4001},
        {"name": "Evil"},
        {"role": "owner", "prompt": "x"},
        {"handles": {}},
    ],
)
def test_a_bad_body_is_400_and_writes_nothing(client, data_dir, body) -> None:
    response = client.post("/people/robin/profile", headers=_h(), json=body)
    assert response.status_code == 400
    assert "error" in response.json()
    assert not (data_dir / "people.yaml").exists()


def test_a_body_that_is_not_an_object_is_400(client) -> None:
    assert client.post("/people/robin/profile", headers=_h(), json=["a"]).status_code == 400
    assert client.post("/people/robin/profile", headers=_h(), content=b"nope").status_code == 400


def test_a_prompt_at_the_cap_is_kept(client) -> None:
    response = client.post("/people/robin/profile", headers=_h(), json={"prompt": "x" * 4000})
    assert response.status_code == 200


def test_the_caller_check(client, monkeypatch) -> None:
    body = {"journal": "off"}
    assert client.get("/people/robin/profile").status_code == 401
    assert client.post("/people/robin/profile", json=body).status_code == 401
    assert client.get("/people/robin/profile", headers=_h("bad")).status_code == 401
    assert client.get("/people/robin/profile", headers=_h(MCP)).status_code == 403
    assert client.post("/people/robin/profile", headers=_h(MCP), json=body).status_code == 403
    monkeypatch.setenv("AUTH_CALLERS", "")
    assert client.get("/people/robin/profile", headers=_h()).status_code == 403
    monkeypatch.setenv("AUTH_CALLERS", "viewer,mcp")
    assert client.get("/people/robin/profile", headers=_h(MCP)).status_code == 200


def test_the_router_names_the_documented_routes() -> None:
    router = build_profile_router()
    assert {(r.path, tuple(sorted(r.methods))) for r in router.routes} == {
        ("/people/{person_id}/profile", ("GET",)),
        ("/people/{person_id}/profile", ("POST",)),
    }


def test_a_role_in_the_file_entry_survives_a_profile_write(client, data_dir) -> None:
    from joshua_core.store import people_file

    people_file.set_role(data_dir / "people.yaml", "robin", "member")
    client.post("/people/robin/profile", headers=_h(), json={"journal": "off"})
    [entry] = _file(data_dir)
    assert entry["role"] == "member" and entry["journal"] == "off"
    robin = config_module.reload(Path(config_module._resolve_path(None))).person("robin")
    assert robin.role == "member" and robin.name == "Robin"


def test_a_missing_snippet_file_reads_as_null(client, data_dir) -> None:
    (data_dir.parent / "robin-config.md").unlink()
    assert client.get("/people/robin/profile", headers=_h()).json()["prompt"] is None

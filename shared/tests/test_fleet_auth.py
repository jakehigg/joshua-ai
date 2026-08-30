import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from joshua_shared import fleet_auth

TOKENS = {"core": "tok-core", "channels": "tok-channels", "laptop": "tok-laptop"}


def test_load_fleet_tokens_maps_and_lowercases() -> None:
    env = {"JOSHUA_TOKEN_CORE": "a", "JOSHUA_TOKEN_CHANNELS": "b", "OTHER": "c"}
    assert fleet_auth.load_fleet_tokens(env) == {"core": "a", "channels": "b"}


def test_load_fleet_tokens_skips_empty() -> None:
    env = {"JOSHUA_TOKEN_CORE": "a", "JOSHUA_TOKEN_GATEWAY": ""}
    assert fleet_auth.load_fleet_tokens(env) == {"core": "a"}


@pytest.mark.parametrize(
    "header",
    [None, "", "tok-core", "Basic tok-core", "Bearer ", "Bearer wrong"],
)
def test_identify_bearer_rejects_missing_malformed_and_unknown(header: str | None) -> None:
    assert fleet_auth.identify_bearer(header, TOKENS) is None


def test_identify_bearer_returns_identity_for_valid_token() -> None:
    assert fleet_auth.identify_bearer("Bearer tok-core", TOKENS) == "core"
    assert fleet_auth.identify_bearer("Bearer tok-channels", TOKENS) == "channels"


def test_authenticate_status_codes() -> None:
    assert fleet_auth.authenticate("Bearer nope", TOKENS, allowed=None) == (401, None)
    assert fleet_auth.authenticate("Bearer tok-core", TOKENS, allowed=None) == (200, "core")
    assert fleet_auth.authenticate("Bearer tok-core", TOKENS, allowed=["channels"]) == (
        403,
        "core",
    )
    assert fleet_auth.authenticate("Bearer tok-channels", TOKENS, allowed=["channels"]) == (
        200,
        "channels",
    )


def test_authenticate_empty_allowlist_admits_any_identity() -> None:
    assert fleet_auth.authenticate("Bearer tok-core", TOKENS, allowed=[]) == (200, "core")


def _app(dependency) -> TestClient:
    app = FastAPI()

    @app.get("/y")
    def guarded(identity: str = Depends(dependency)):
        return {"identity": identity}

    return TestClient(app, raise_server_exceptions=True)


def test_require_identity_allows_and_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "tok-core")
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", "tok-channels")
    client = _app(fleet_auth.require_identity(allowed=["channels"]))

    assert client.get("/y").status_code == 401
    assert client.get("/y", headers={"Authorization": "Bearer bad"}).status_code == 401
    core = client.get("/y", headers={"Authorization": "Bearer tok-core"})
    assert core.status_code == 403
    ok = client.get("/y", headers={"Authorization": "Bearer tok-channels"})
    assert ok.status_code == 200
    assert ok.json() == {"identity": "channels"}


def test_require_identity_admin_denies_valid_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "tok-core")
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", "tok-laptop")
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    client = _app(fleet_auth.require_identity(admin=True))

    # core is a valid fleet identity but not in the default ADMIN_CALLERS.
    denied = client.get("/y", headers={"Authorization": "Bearer tok-core"})
    assert denied.status_code == 403
    allowed = client.get("/y", headers={"Authorization": "Bearer tok-laptop"})
    assert allowed.status_code == 200
    assert allowed.json() == {"identity": "laptop"}


def test_require_identity_admin_reads_admin_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "tok-core")
    monkeypatch.setenv("ADMIN_CALLERS", "core")
    client = _app(fleet_auth.require_identity(admin=True))
    ok = client.get("/y", headers={"Authorization": "Bearer tok-core"})
    assert ok.status_code == 200


def test_require_identity_raises_httpexception_directly() -> None:
    dep = fleet_auth.require_identity(allowed=["channels"])

    class _Req:
        headers: dict[str, str] = {}

    with pytest.raises(HTTPException) as exc:
        dep(_Req())  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_identity_from_scope() -> None:
    scope = {"headers": [(b"authorization", b"Bearer tok-core"), (b"x-other", b"1")]}
    assert fleet_auth.identity_from_scope(scope, TOKENS) == "core"
    assert fleet_auth.identity_from_scope({"headers": []}, TOKENS) is None
    bad = {"headers": [(b"authorization", b"Bearer nope")]}
    assert fleet_auth.identity_from_scope(bad, TOKENS) is None

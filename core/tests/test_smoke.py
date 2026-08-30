import joshua_core
from fastapi.testclient import TestClient
from joshua_core.main import build_app


def test_import_and_version() -> None:
    assert joshua_core.__version__


def test_healthz() -> None:
    # No context manager, so the DB-backed lifespan does not run.
    response = TestClient(build_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

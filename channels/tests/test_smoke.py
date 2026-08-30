import joshua_channels
from fastapi.testclient import TestClient
from joshua_channels.app import app


def test_import_and_version() -> None:
    assert joshua_channels.__version__


def test_healthz() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

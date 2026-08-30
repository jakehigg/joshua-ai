import joshua_gateway
from joshua_gateway.main import build_app


def test_import_and_version() -> None:
    assert joshua_gateway.__version__


def test_build_app_has_base_routes() -> None:
    app = build_app()
    paths = {getattr(route, "path", None) for route in app.router.routes}
    assert {"/healthz", "/readyz", "/admin/reload", "/admin/calls", "/admin/inventory"} <= paths

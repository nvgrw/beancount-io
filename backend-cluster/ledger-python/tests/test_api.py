import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


IDL_PATH = Path(__file__).resolve().parents[2] / "idl" / "beancount-ledger.openapi.json"


def _normalized_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_healthz_identifies_python_engine() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["data"]["engine"] == "python-beancount"


def test_routes_match_canonical_ledger_contract() -> None:
    specification = json.loads(IDL_PATH.read_text(encoding="utf-8"))
    expected = {
        (method.upper(), _normalized_path(path))
        for path, methods in specification["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "delete", "patch"}
    }
    actual = {
        (method, _normalized_path(route.path))
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}
        and route.path
        not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    }

    assert actual == expected


def test_report_requires_forwardable_credentials() -> None:
    response = TestClient(app).get("/reports/alice/book/errors")

    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "error": "No authorization header provided. Use 'Basic <credentials>' or 'token <token>'",
        "code": None,
        "details": None,
    }

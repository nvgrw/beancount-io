from fastapi.testclient import TestClient

from app.main import app


def test_healthz_identifies_python_engine() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["data"]["engine"] == "python-beancount"


def test_report_requires_forwardable_credentials() -> None:
    response = TestClient(app).get("/reports/alice/book/errors")

    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "error": "No authorization header provided. Use 'Basic <credentials>' or 'token <token>'",
        "code": None,
        "details": None,
    }

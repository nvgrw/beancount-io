import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import shell
from app.ledger import load_snapshot
from app.main import app
from app.repository import RepositorySnapshot


SHADOW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "ledger"
    / "scripts"
    / "rustledger-shadow"
)


def test_shell_routes_match_python_oracle() -> None:
    value = load_snapshot(
        RepositorySnapshot(
            owner="alice",
            repo="shadow",
            sha="fixture",
            root=SHADOW_ROOT / "fixtures",
            entrypoint="main.bean",
        )
    )
    expected = json.loads(
        (SHADOW_ROOT / "python-oracle.json").read_text(encoding="utf-8")
    )["responses"]
    query = "SELECT date, account, number WHERE account ~ 'Depenses' ORDER BY date"

    app.dependency_overrides[shell.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            structured = client.get(
                "/shell/alice/shadow/query", params={"query": query}
            )
            structured_post = client.post(
                "/shell/alice/shadow/query", json={"query": query}
            )
            text = client.get(
                "/shell/alice/shadow/query-text", params={"query": query}
            )
            text_post = client.post(
                "/shell/alice/shadow/query-text", json={"query": query}
            )
        assert structured.status_code == 200, structured.text
        assert structured_post.status_code == 200, structured_post.text
        assert text.status_code == 200, text.text
        assert text_post.status_code == 200, text_post.text
        assert structured.json()["data"] == expected["queryShell"]
        assert structured_post.json()["data"] == expected["queryShell"]
        assert text.json()["data"] == {
            **expected["queryShellText"],
            "rowCount": 2,
        }
        assert text_post.json()["data"] == text.json()["data"]
    finally:
        app.dependency_overrides.pop(shell.loaded, None)

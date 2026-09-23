import base64
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app import legacy, webhooks
from app.gitea import GiteaClient
from app.ledger import load_snapshot
from app.main import app
from app.repository import RepositorySnapshot


AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
SHADOW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "ledger"
    / "scripts"
    / "rustledger-shadow"
)


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def test_bulk_entries_format_and_commit_atomically(monkeypatch) -> None:
    current = 'option "operating_currency" "USD"\n'
    requests: list[tuple[str, str, object]] = []

    async def request(self, method, path, *, json=None, params=None):
        requests.append((method, path, json))
        if method == "GET":
            return {
                "sha": "blob-sha",
                "content": base64.b64encode(current.encode()).decode(),
            }
        return None

    monkeypatch.setattr(GiteaClient, "request", request)
    response = TestClient(app).post(
        "/entries/alice/book/bulk",
        headers=AUTH,
        json={
            "entries": [
                {
                    "type": "transaction",
                    "item": {
                        "date": "2026-09-23",
                        "flag": "*",
                        "payee": "Cafe",
                        "narration": "Lunch",
                        "postings": [
                            {
                                "account": "Expenses:Food",
                                "units": {"number": "12.50", "currency": "USD"},
                            },
                            {"account": "Assets:Cash"},
                        ],
                    },
                },
                {
                    "type": "document",
                    "item": {
                        "date": "2026-09-23",
                        "account": "Assets:Cash",
                        "filename": "receipts/lunch.pdf",
                        "tags": ["receipt"],
                    },
                },
                {
                    "type": "custom",
                    "item": {
                        "date": "2026-09-23",
                        "type": "budget",
                        "values": [
                            {"kind": "account", "value": "Expenses:Food"},
                            {"kind": "amount", "number": "100", "currency": "USD"},
                        ],
                    },
                },
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert [method for method, _, _ in requests] == ["GET", "POST"]
    operation = requests[-1][2]["files"][0]
    committed = base64.b64decode(operation["content"]).decode()
    assert '2026-09-23 * "Cafe" "Lunch"' in committed
    assert "  Assets:Cash" in committed
    assert '2026-09-23 document Assets:Cash "receipts/lunch.pdf" #receipt' in committed
    assert '2026-09-23 custom "budget" Expenses:Food 100 USD' in committed
    assert operation["sha"] == "blob-sha"


def test_bulk_entries_reject_unbalanced_transaction(monkeypatch) -> None:
    called = False

    async def request(self, method, path, *, json=None, params=None):
        nonlocal called
        called = True

    monkeypatch.setattr(GiteaClient, "request", request)
    response = TestClient(app).post(
        "/entries/alice/book/bulk",
        headers=AUTH,
        json={
            "entries": [
                {
                    "type": "transaction",
                    "item": {
                        "date": "2026-09-23",
                        "flag": "*",
                        "postings": [
                            {
                                "account": "Expenses:Food",
                                "units": {"number": "12.50", "currency": "USD"},
                            },
                            {
                                "account": "Assets:Cash",
                                "units": {"number": "-10", "currency": "USD"},
                            },
                        ],
                    },
                }
            ]
        },
    )

    assert response.status_code == 400
    assert "Transaction does not balance" in response.json()["error"]
    assert called is False


def test_legacy_journal_filters_and_details() -> None:
    value = load_snapshot(
        RepositorySnapshot(
            owner="alice",
            repo="shadow",
            sha="fixture",
            root=SHADOW_ROOT / "fixtures",
            entrypoint="main.bean",
        )
    )
    app.dependency_overrides[legacy.loaded] = lambda: value
    try:
        response = TestClient(app).get(
            "/legacy/journal/alice/shadow",
            headers=AUTH,
            params={
                "search_query": "lunch",
                "entry_types": "Transaction",
                "sort_order": "asc",
                "detailed": "true",
            },
        )
    finally:
        app.dependency_overrides.pop(legacy.loaded, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [entry["narration"] for entry in data] == [
        "Lunch #food",
        "Lunch again #food",
    ]
    assert all(entry["entry_type"] == "transaction" for entry in data)
    assert all("entry_hash" in entry for entry in data)


def test_archive_forwards_bytes_and_headers(monkeypatch) -> None:
    async def raw_request(self, method, path, *, json=None, params=None):
        return httpx.Response(
            200,
            content=b"archive-bytes",
            headers={
                "content-type": "application/zip",
                "content-disposition": 'attachment; filename="book.zip"',
            },
        )

    monkeypatch.setattr(GiteaClient, "raw_request", raw_request)
    response = TestClient(app).get(
        "/ledgers/alice/book/archive/book.zip", headers=AUTH
    )

    assert response.status_code == 200
    assert response.content == b"archive-bytes"
    assert response.headers["content-type"] == "application/zip"
    assert "book.zip" in response.headers["content-disposition"]


def test_webhooks_authenticate_and_count_archives(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "settings", SimpleNamespace(webhook_token="secret"))
    client = TestClient(app)
    denied = client.post(
        "/webhook/gitea/repo-push",
        headers={"Authorization": "Bearer wrong"},
        json={"repository": {"full_name": "alice/book"}, "sender": {"username": "alice"}},
    )
    assert denied.status_code == 401

    new = _archive(
        {
            "main.bean": (
                "2024-01-01 open Assets:Cash USD\n"
                "2024-02-01 open Assets:Savings USD\n"
            )
        }
    )
    old = _archive({"main.bean": "2024-01-01 open Assets:Cash USD\n"})
    response = client.post(
        "/webhook/gitea/pre-receive-check",
        headers={"Authorization": "Bearer secret"},
        data={"owner": "alice", "repo": "book"},
        files={
            "archive": ("archive.tar.gz", new, "application/gzip"),
            "old_archive": ("old.tar.gz", old, "application/gzip"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "allow": True,
        "reason": "unlimited_or_check_unavailable",
        "new_directive_count": 2,
        "old_directive_count": 1,
        "limit": None,
    }

    corrupt = client.post(
        "/webhook/gitea/pre-receive-check",
        headers={"Authorization": "Bearer secret"},
        data={"owner": "alice", "repo": "book"},
        files={"archive": ("archive.tar.gz", b"garbage", "application/gzip")},
    )
    assert corrupt.status_code == 200
    assert corrupt.json()["data"]["reason"] == "check_error"

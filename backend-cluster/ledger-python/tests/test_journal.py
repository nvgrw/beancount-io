import json
import base64
import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from fava.beans.funcs import hash_entry

from app import journal
from app.gitea import GiteaClient
from app.ledger import load_snapshot
from app.main import app
from app.repository import RepositorySnapshot


SHADOW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "ledger"
    / "scripts"
    / "rustledger-shadow"
)
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}


def _fixture_ledger():
    return load_snapshot(
        RepositorySnapshot(
            owner="alice",
            repo="shadow",
            sha="fixture",
            root=SHADOW_ROOT / "fixtures",
            entrypoint="main.bean",
        )
    )


def _mask_entry_hashes(value: Any) -> Any:
    if isinstance(value, list):
        return [_mask_entry_hashes(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<entry-hash>" if key == "entry_hash" else _mask_entry_hashes(item)
            for key, item in value.items()
        }
    return value


def test_journal_reads_match_python_oracle() -> None:
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
    requests = {
        "getJournal": (
            "/journal/alice/shadow",
            {"time": "2024", "limit": 100},
        ),
        "plaintextJournal": (
            "/journal/alice/shadow/plaintext",
            {"time": "2024"},
        ),
        "getAccountJournal": (
            "/journal/alice/shadow/account-journal",
            {
                "account": "Actifs:Cash",
                "conversion": "units",
                "time": "2024",
                "limit": 100,
            },
        ),
    }

    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            for operation, (path, params) in requests.items():
                response = client.get(path, params=params)
                assert response.status_code == 200, response.text
                assert _mask_entry_hashes(response.json()["data"]) == (
                    _mask_entry_hashes(expected[operation])
                )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)


def test_context_returns_source_and_balances() -> None:
    value = _fixture_ledger()
    entry = next(
        item
        for item in value.entries
        if getattr(item, "narration", None) == "Lunch"
    )
    entry_id = hash_entry(entry)
    expected_slice = (
        '2024-01-20 * "Cafe" "Lunch" #food\n'
        "  Depenses:Food      25.50 USD\n"
        "  Actifs:Cash       -25.50 USD"
    )

    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        response = TestClient(app).get(
            f"/journal/alice/shadow/context/{entry_id}"
        )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["slice"] == expected_slice
    assert data["balances_before"] == {
        "Actifs:Cash": ["800.00 USD"],
        "Depenses:Food": [],
    }
    assert data["balances_after"] == {
        "Actifs:Cash": ["774.50 USD"],
        "Depenses:Food": ["25.50 USD"],
    }


def test_update_source_slice_commits_and_returns_new_entry_id(monkeypatch) -> None:
    value = _fixture_ledger()
    entry = next(
        item for item in value.entries if getattr(item, "narration", None) == "Lunch"
    )
    entry_id = hash_entry(entry)
    source, source_sha = journal._source_slice(value, entry)
    current = (value.snapshot.root / "books/2024.bean").read_text(encoding="utf-8")
    requests: list[tuple[str, str, Any]] = []

    async def request(self, method, path, *, json=None, params=None):
        requests.append((method, path, json))
        if method == "GET":
            return {
                "sha": "blob-sha",
                "content": base64.b64encode(current.encode()).decode(),
            }
        return {"content": {"sha": "new-blob-sha"}}

    monkeypatch.setattr(GiteaClient, "request", request)
    updated_source = source.replace('"Lunch"', '"Team lunch"')
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        response = TestClient(app).put(
            "/journal/alice/shadow/source-slice",
            headers=AUTH,
            json={
                "entry_hash": entry_id,
                "sha256sum": source_sha,
                "new_content": updated_source,
            },
        )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["entry_hash"] == data["new_entry_hash"]
    assert data["entry_hash"] != entry_id
    assert data["new_sha256sum"] == hashlib.sha256(updated_source.encode()).hexdigest()
    assert requests[-1][0] == "PUT"
    assert requests[-1][2]["sha"] == "blob-sha"
    committed = base64.b64decode(requests[-1][2]["content"]).decode()
    assert '"Team lunch"' in committed
    assert '"Lunch" #food' not in committed


def test_delete_source_slice_rejects_stale_source(monkeypatch) -> None:
    value = _fixture_ledger()
    entry = next(
        item for item in value.entries if getattr(item, "narration", None) == "Lunch"
    )
    current = (value.snapshot.root / "books/2024.bean").read_text(encoding="utf-8")
    methods: list[str] = []

    async def request(self, method, path, *, json=None, params=None):
        methods.append(method)
        return {
            "sha": "blob-sha",
            "content": base64.b64encode(current.encode()).decode(),
        }

    monkeypatch.setattr(GiteaClient, "request", request)
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        response = TestClient(app).request(
            "DELETE",
            "/journal/alice/shadow/source-slice",
            headers=AUTH,
            json={"entry_hash": hash_entry(entry), "sha256sum": "stale"},
        )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert response.status_code == 409
    assert methods == ["GET"]


def test_delete_multiple_source_slices_uses_one_atomic_commit(monkeypatch) -> None:
    value = _fixture_ledger()
    entries = [
        item
        for item in value.entries
        if getattr(item, "narration", None) in {"Lunch", "Lunch again"}
    ]
    current = (value.snapshot.root / "books/2024.bean").read_text(encoding="utf-8")
    requests: list[tuple[str, Any]] = []

    async def request(self, method, path, *, json=None, params=None):
        requests.append((method, json))
        if method == "GET":
            return {
                "sha": "blob-sha",
                "content": base64.b64encode(current.encode()).decode(),
            }
        return None

    monkeypatch.setattr(GiteaClient, "request", request)
    body = {
        "entries": [
            {
                "entry_hash": hash_entry(entry),
                "sha256sum": journal._source_slice(value, entry)[1],
            }
            for entry in entries
        ]
    }
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        response = TestClient(app).request(
            "DELETE",
            "/journal/alice/shadow/source-slices",
            headers=AUTH,
            json=body,
        )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert response.status_code == 200, response.text
    assert [method for method, _ in requests] == ["GET", "POST"]
    operation = requests[-1][1]["files"][0]
    committed = base64.b64decode(operation["content"]).decode()
    assert '"Lunch"' not in committed
    assert '"Lunch again"' not in committed
    assert operation["sha"] == "blob-sha"

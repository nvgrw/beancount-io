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
                actual = response.json()["data"]
                if operation == "plaintextJournal":
                    assert actual["total"] == 4
                    assert actual["truncated"] is False
                    actual = {"content": actual["content"]}
                assert _mask_entry_hashes(actual) == (
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
    assert data["managed_source"] is None


def test_managed_price_context_is_read_only(monkeypatch) -> None:
    value = _fixture_ledger()
    entry = next(
        item for item in value.entries if getattr(item, "narration", None) == "Lunch"
    )
    entry_id = hash_entry(entry)
    source, source_sha = journal._source_slice(value, entry)
    url = "https://beancount.io/prices/BTC-USD"
    requests: list[str] = []

    async def request(self, method, path, *, json=None, params=None):
        requests.append(method)
        return None

    monkeypatch.setattr(journal, "managed_source_for_path", lambda _path: url)
    monkeypatch.setattr(GiteaClient, "request", request)
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            context = client.get(f"/journal/alice/shadow/context/{entry_id}")
            update = client.put(
                "/journal/alice/shadow/source-slice",
                headers=AUTH,
                json={
                    "entry_hash": entry_id,
                    "sha256sum": source_sha,
                    "new_content": source.replace('"Lunch"', '"Team lunch"'),
                },
            )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert context.status_code == 200, context.text
    assert context.json()["data"]["managed_source"] == url
    assert update.status_code == 400
    assert "read-only" in update.json()["error"]
    assert requests == []


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


def test_journal_include_source_and_plaintext_pagination() -> None:
    value = _fixture_ledger()
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            jr = client.get(
                "/journal/alice/shadow",
                params={"time": "2024", "limit": 5, "include_source": "true"},
            )
            pt = client.get(
                "/journal/alice/shadow/plaintext",
                params={"time": "2024", "limit": 2, "offset": 1},
            )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert jr.status_code == 200, jr.text
    items = jr.json()["data"]["items"]
    assert len(items) == 5
    assert all("source" in item for item in items)
    assert items[0]["source"]["path"].endswith(".bean")
    assert items[0]["source"]["lineno"] >= 1
    assert pt.status_code == 200, pt.text
    assert pt.json()["data"]["total"] == 4
    assert pt.json()["data"]["truncated"] is True


def test_move_source_slices_and_update_dry_run(tmp_path: Path, monkeypatch) -> None:
    main_text = (
        'option "operating_currency" "USD"\n'
        '2024-01-01 open Assets:Cash USD\n'
        '2024-01-01 open Expenses:Food USD\n'
        'include "txns.bean"\n'
    )
    txns_text = (
        "pushtag #trip\n"
        '2024-01-20 * "Cafe" "Lunch"\n'
        "  Expenses:Food  25.50 USD\n"
        "  Assets:Cash   -25.50 USD\n"
        "poptag #trip\n"
    )
    (tmp_path / "main.bean").write_text(main_text, encoding="utf-8")
    (tmp_path / "txns.bean").write_text(txns_text, encoding="utf-8")
    value = load_snapshot(
        RepositorySnapshot(
            owner="alice",
            repo="shadow",
            sha="fixture",
            root=tmp_path,
            entrypoint="main.bean",
        )
    )
    entry = next(
        item for item in value.entries if getattr(item, "narration", None) == "Lunch"
    )
    entry_id = hash_entry(entry)
    source, source_sha = journal._source_slice(value, entry)
    requests: list[tuple[str, str, Any]] = []

    async def request(self, method, path, *, json=None, params=None):
        requests.append((method, path, json))
        if method == "GET":
            if path.endswith("txns.bean"):
                return {
                    "sha": "sha-txns",
                    "content": base64.b64encode(txns_text.encode()).decode(),
                }
            if path.endswith("main.bean"):
                return {
                    "sha": "sha-main",
                    "content": base64.b64encode(main_text.encode()).decode(),
                }
            from app.errors import ServiceError

            raise ServiceError(404, "not found")
        return {"commit": {"sha": "commit-move-123"}}

    monkeypatch.setattr(GiteaClient, "request", request)
    app.dependency_overrides[journal.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            dry = client.put(
                "/journal/alice/shadow/source-slice",
                headers=AUTH,
                json={
                    "entry_hash": entry_id,
                    "sha256sum": source_sha,
                    "new_content": source,
                    "dryRun": True,
                },
            )
            moved = client.post(
                "/journal/alice/shadow/move-source-slices",
                headers=AUTH,
                json={
                    "entries": [{"entry_hash": entry_id, "sha256sum": source_sha}],
                    "to_path": "archive/moved.bean",
                    "include_from": "main.bean",
                },
            )
    finally:
        app.dependency_overrides.pop(journal.loaded, None)

    assert dry.status_code == 200, dry.text
    dry_data = dry.json()["data"]
    assert dry_data["unchanged"] is True
    assert "preview" in dry_data
    assert "commit" not in dry_data
    assert moved.status_code == 200, moved.text
    moved_data = moved.json()["data"]
    assert moved_data["moved_hashes"] == [entry_id]
    assert moved_data["include_added"] is True
    assert moved_data["commit"] == "commit-move-123"
    assert set(moved_data["files"]) == {"txns.bean", "archive/moved.bean", "main.bean"}
    post_payload = requests[-1][2]
    by_path = {op["path"]: op for op in post_payload["files"]}
    assert by_path["archive/moved.bean"]["operation"] == "create"
    moved_content = base64.b64decode(by_path["archive/moved.bean"]["content"]).decode()
    assert '"Lunch" #trip' in moved_content
    assert 'include "archive/moved.bean"' in base64.b64decode(by_path["main.bean"]["content"]).decode()

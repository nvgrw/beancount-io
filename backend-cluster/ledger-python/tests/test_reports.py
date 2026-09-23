import json
from pathlib import Path
from typing import Any

from beancount.core.data import Open
from beancount.parser import options as options_lib
from fastapi.testclient import TestClient

from app import reports
from app.ledger import load_snapshot
from app.main import app
from app.repository import RepositorySnapshot


SHADOW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "ledger"
    / "scripts"
    / "rustledger-shadow"
)


def _without_empty_clamp_phantoms(value: Any, loaded: Any) -> Any:
    opened = {entry.account for entry in loaded.entries if isinstance(entry, Open)}
    hidden = set(options_lib.get_previous_accounts(loaded.options)) - opened

    def clean(item: Any) -> Any:
        if isinstance(item, list):
            return [
                cleaned
                for child in item
                if not (
                    isinstance((cleaned := clean(child)), dict)
                    and cleaned.get("account") in hidden
                    and not cleaned.get("balance")
                    and not cleaned.get("balance_children")
                    and not cleaned.get("children")
                )
            ]
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        return item

    return clean(value)


def _mask_entry_hashes(value: Any) -> Any:
    if isinstance(value, list):
        return [_mask_entry_hashes(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<entry-hash>" if key == "entry_hash" else _mask_entry_hashes(item)
            for key, item in value.items()
        }
    return value


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


def test_financial_reports_match_python_oracle() -> None:
    value = _fixture_ledger()
    expected = json.loads(
        (SHADOW_ROOT / "python-oracle.json").read_text(encoding="utf-8")
    )["responses"]
    requests = {
        "getLedgerHierarchy": (
            "/reports/alice/shadow/hierarchy",
            {"account_name": "Actifs", "conversion": "USD"},
        ),
        "getLedgerIntervalTotals": (
            "/reports/alice/shadow/interval-totals",
            {
                "account_name": "Depenses",
                "conversion": "USD",
                "interval": "month",
                "time": "2024",
            },
        ),
        "getLedgerAccountReport": (
            "/reports/alice/shadow/account_report",
            {
                "account_name": "Actifs",
                "conversion": "USD",
                "interval": "month",
                "time": "2024",
            },
        ),
        "getLedgerOverview": (
            "/reports/alice/shadow/overview",
            {"conversion": "USD", "interval": "month", "time": "2024"},
        ),
        "getLedgerIncomeStatement": (
            "/reports/alice/shadow/income-statement",
            {"conversion": "USD", "interval": "month", "time": "2024"},
        ),
        "getLedgerBalanceSheet": (
            "/reports/alice/shadow/balance-sheet",
            {"conversion": "USD", "interval": "month", "time": "2024"},
        ),
        "getLedgerTrialBalance": (
            "/reports/alice/shadow/trial-balance",
            {"conversion": "USD", "time": "2024"},
        ),
    }

    app.dependency_overrides[reports.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            for operation, (path, params) in requests.items():
                response = client.get(path, params=params)
                assert response.status_code == 200, response.text
                actual = response.json()["data"]
                if operation == "getLedgerTrialBalance":
                    assert actual["valuation_adjustment"] == {"USD": "-100.00"}
                    assert actual["rounding_residual"] == {}
                    actual = {
                        k: v
                        for k, v in actual.items()
                        if k not in {"valuation_adjustment", "rounding_residual"}
                    }
                assert actual == _without_empty_clamp_phantoms(
                    expected[operation], value
                )
    finally:
        app.dependency_overrides.pop(reports.loaded, None)


def test_data_reports_match_python_oracle() -> None:
    value = _fixture_ledger()
    expected = json.loads(
        (SHADOW_ROOT / "python-oracle.json").read_text(encoding="utf-8")
    )["responses"]
    base = "/reports/alice/shadow"
    requests = {
        "getLedgerAttributes": (f"{base}/attributes", {}),
        "getLedgerOptions": (f"{base}/options", {}),
        "getLedgerFavaOptions": (f"{base}/fava-options", {}),
        "getLedgerBcioOptions": (f"{base}/beancountio-options", {}),
        "getLedgerPlugins": (f"{base}/plugins", {}),
        "getLedgerSourceFiles": (f"{base}/source-files", {}),
        "getLedgerCommodities": (f"{base}/commodities", {}),
        "getLedgerPayeeTransactions": (
            f"{base}/payee-transactions",
            {"payee": "Cafe"},
        ),
        "getLedgerNarrationTransactions": (
            f"{base}/narration-transactions",
            {"narration": "Lunch"},
        ),
        "getLedgerPayeeAccounts": (f"{base}/payee-accounts", {"payee": "Cafe"}),
        "getLedgerEvents": (f"{base}/events", {"time": "2024"}),
        "getLedgerDocuments": (f"{base}/documents", {"time": "2024"}),
        "getLedgerPayees": (f"{base}/payees", {}),
        "getLedgerNarrations": (f"{base}/narrations", {}),
        "getLedgerAccounts": (f"{base}/accounts", {}),
        "getLedgerLinks": (f"{base}/links", {}),
        "getLedgerYears": (f"{base}/years", {}),
        "getLedgerCurrencies": (f"{base}/currencies", {}),
        "getLedgerTags": (f"{base}/tags", {}),
        "getLedgerErrors": (f"{base}/errors", {}),
        "getLedgerAccountLastEntries": (
            f"{base}/account_last_entries",
            {"time": "2024"},
        ),
        "getLedgerEntriesCountPerType": (
            f"{base}/entries_count_per_type",
            {"time": "2024"},
        ),
    }

    app.dependency_overrides[reports.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            for operation, (path, params) in requests.items():
                response = client.get(path, params=params)
                assert response.status_code == 200, response.text
                assert _mask_entry_hashes(response.json()["data"]) == (
                    _mask_entry_hashes(expected[operation])
                )

            postings = client.get(f"{base}/postings_per_account")
        assert postings.status_code == 200, postings.text
        assert postings.json()["data"] == [
            {"account": "Actifs:Brokerage", "count": 1},
            {"account": "Actifs:Cash", "count": 5},
            {"account": "Depenses:Food", "count": 2},
            {"account": "Revenus:Salary", "count": 2},
        ]
    finally:
        app.dependency_overrides.pop(reports.loaded, None)


def test_managed_price_status_and_refresh_routes(monkeypatch) -> None:
    value = _fixture_ledger()
    url = "https://beancount.io/prices/BTC-USD"
    value.options["bea_managed_price_sources"] = [
        {"url": url, "revision": "r1"}
    ]
    calls: list[tuple[str, ...]] = []
    invalidated = []

    def read_sources(_snapshot, *, refresh_urls=()):
        calls.append(tuple(refresh_urls))
        return [{"url": url, "revision": "r1", "freshness": "recent"}]

    async def invalidate(snapshot):
        invalidated.append(snapshot)

    monkeypatch.setattr(reports, "read_managed_price_sources", read_sources)
    monkeypatch.setattr(reports.parsed_ledger_cache, "invalidate", invalidate)
    app.dependency_overrides[reports.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            status = client.get("/reports/alice/shadow/managed-prices")
            refresh = client.post("/reports/alice/shadow/managed-prices/refresh")
    finally:
        app.dependency_overrides.pop(reports.loaded, None)

    assert status.status_code == 200, status.text
    assert refresh.status_code == 200, refresh.text
    assert status.json()["data"][0]["revision"] == "r1"
    assert refresh.json()["data"][0]["revision"] == "r1"
    assert calls == [(), (url,)]
    assert invalidated == [value.snapshot]


def test_check_files_and_projected_reports() -> None:
    import base64

    auth = {"Authorization": "Basic dGVzdDp0ZXN0"}
    valid_main = base64.b64encode(
        b'option "operating_currency" "USD"\n2024-01-01 open Assets:Cash USD\n'
    ).decode()
    invalid_main = base64.b64encode(
        b'2024-01-01 * "Broken"\n  Assets:Cash  10 USD\n  Expenses:Food  -5 USD\n'
    ).decode()

    with TestClient(app) as client:
        ok = client.post(
            "/reports/check-files",
            headers=auth,
            json={"files": [{"path": "main.bean", "content": valid_main}]},
        )
        bad = client.post(
            "/reports/check-files",
            headers=auth,
            json={"files": [{"path": "main.bean", "content": invalid_main}]},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"] == []
    assert bad.status_code == 200, bad.text
    assert len(bad.json()["data"]) >= 1
    assert bad.json()["data"][0]["severity"] == "error"

    value = _fixture_ledger()
    extra_bean = base64.b64encode(
        b'2024-06-01 * "Extra" "Coffee"\n  Depenses:Food  4.00 USD\n  Actifs:Cash   -4.00 USD\n'
    ).decode()
    updated_main = (
        (value.snapshot.root / "main.bean").read_text(encoding="utf-8")
        + '\ninclude "books/extra.bean"\n'
    )
    app.dependency_overrides[reports.loaded] = lambda: value
    try:
        with TestClient(app) as client:
            src_resp = client.post(
                "/reports/alice/shadow/source-files",
                headers=auth,
                json={
                    "files": [
                        {
                            "path": "main.bean",
                            "content": base64.b64encode(updated_main.encode()).decode(),
                        },
                        {"path": "books/extra.bean", "content": extra_bean},
                    ]
                },
            )
            ids_resp = client.post(
                "/reports/alice/shadow/entry-ids",
                headers=auth,
                json={
                    "files": [
                        {
                            "path": "main.bean",
                            "content": base64.b64encode(updated_main.encode()).decode(),
                        },
                        {"path": "books/extra.bean", "content": extra_bean},
                    ],
                    "locations": [
                        {"path": "books/extra.bean", "line": 1},
                        {"path": "books/extra.bean", "line": 99},
                    ],
                },
            )
    finally:
        app.dependency_overrides.pop(reports.loaded, None)

    assert src_resp.status_code == 200, src_resp.text
    assert "books/extra.bean" in src_resp.json()["data"]
    assert ids_resp.status_code == 200, ids_resp.text
    entry_hashes = ids_resp.json()["data"]["entry_hashes"]
    assert len(entry_hashes) == 2
    assert isinstance(entry_hashes[0], str) and len(entry_hashes[0]) > 0
    assert entry_hashes[1] is None

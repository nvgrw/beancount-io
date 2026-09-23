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
                assert response.json()["data"] == _without_empty_clamp_phantoms(
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

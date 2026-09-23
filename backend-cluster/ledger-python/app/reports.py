from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import tempfile
from pathlib import Path
from typing import Annotated, Any

from beancount.core.data import Document, Event, Transaction
from fastapi import APIRouter, Depends
from fava.core.conversion import units
from fava.core.tree import Tree
from fava.modules.chart import ChartModule
from fava.modules.financial_statements import FinancialStatementsModule
from fava.util.date import INTERVALS

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import safe_repo_file_path
from .journal import entry_ids_for_entries
from .ledger import (
    LoadedLedger,
    load_snapshot,
    plugin_names,
    read_managed_price_sources,
    serialize_error,
)
from .main_support import load_ledger, parsed_ledger_cache
from .repository import RepositorySnapshot, resolve_entrypoint
from .serializers import (
    accounts,
    attributes,
    bcio_options,
    beancount_options,
    entries_count,
    fava_options,
    json_value,
    relative_filename,
    source_files,
)
from .workspace import cloned_workspace, write_text_copy_on_write


root_router = APIRouter(prefix="/reports")
router = APIRouter(prefix="/reports/{owner}/{repo}")
MAX_PROJECTED_FILES = 50
MAX_PROJECTED_BYTES = 5_000_000
MAX_LOCATIONS = 200


async def loaded(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> LoadedLedger:
    return await load_ledger(owner, repo, auth)


def _apply_overlays(
    root: Path,
    overlays: Any,
    *,
    allow_delete: bool = True,
) -> None:
    if not isinstance(overlays, list) or len(overlays) > MAX_PROJECTED_FILES:
        raise ServiceError(
            400, f"at most {MAX_PROJECTED_FILES} files may be projected at once"
        )
    total_bytes = 0
    for index, overlay in enumerate(overlays):
        if not isinstance(overlay, dict):
            raise ServiceError(400, f"files[{index}] must name a path")
        path = overlay.get("path")
        safe_repo_file_path(path, f"files[{index}].path")
        target = root / str(path)
        content = overlay.get("content")
        if content is None:
            if not allow_delete:
                raise ServiceError(
                    400, f"files[{index}].content must be base64 UTF-8 text"
                )
            target.unlink(missing_ok=True)
            continue
        if not isinstance(content, str):
            raise ServiceError(
                400, f"files[{index}].content must be base64 text or null"
            )
        try:
            decoded = base64.b64decode(content, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ServiceError(
                400, f"files[{index}].content must be base64 UTF-8 text"
            ) from error
        total_bytes += len(decoded.encode("utf-8"))
        if total_bytes > MAX_PROJECTED_BYTES:
            raise ServiceError(
                400, f"projected contents exceed {MAX_PROJECTED_BYTES} bytes"
            )
        write_text_copy_on_write(target, decoded)


@root_router.post("/check-files")
async def check_ledger_files(
    body: dict[str, Any],
    _auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    files = body.get("files") if isinstance(body, dict) else None
    with tempfile.TemporaryDirectory(prefix="beancount-check-files-") as directory:
        root = Path(directory)
        _apply_overlays(root, files, allow_delete=False)
        entrypoint = resolve_entrypoint(root)
        if not (root / entrypoint).is_file():
            raise ServiceError(400, f"Ledger entry point not found: {entrypoint}")
        snapshot = RepositorySnapshot(
            owner="_check",
            repo="_files",
            sha="check",
            root=root,
            entrypoint=entrypoint,
        )
        projected = await asyncio.to_thread(load_snapshot, snapshot)
        return success(
            [
                serialize_error(error, projected.snapshot.root)
                for error in projected.fava.errors
            ]
        )


@router.get("/errors")
async def errors(value: Annotated[LoadedLedger, Depends(loaded)]) -> dict[str, Any]:
    return success(
        [serialize_error(item, value.snapshot.root) for item in value.fava.errors]
    )


def _managed_records(value: LoadedLedger) -> list[dict[str, Any]]:
    records = value.options.get("bea_managed_price_sources")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _managed_revisions(records: list[dict[str, Any]]) -> dict[str, object]:
    return {
        str(record["url"]): record.get("revision")
        for record in records
        if isinstance(record.get("url"), str)
    }


@router.get("/managed-prices")
async def managed_prices(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    sources = await asyncio.to_thread(read_managed_price_sources, value.snapshot)
    if _managed_revisions(_managed_records(value)) != _managed_revisions(sources):
        await parsed_ledger_cache.invalidate(value.snapshot)
    return success(sources)


@router.post("/managed-prices/refresh")
async def refresh_managed_prices(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    urls = tuple(
        str(record["url"])
        for record in _managed_records(value)
        if isinstance(record.get("url"), str)
    )
    if not urls:
        return success([])
    sources = await asyncio.to_thread(
        read_managed_price_sources,
        value.snapshot,
        refresh_urls=urls,
    )
    await parsed_ledger_cache.invalidate(value.snapshot)
    return success(sources)


@router.post("/check")
async def check_projected_errors(
    body: dict[str, Any],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    overlays = body.get("files") if isinstance(body, dict) else None
    with cloned_workspace(
        value.snapshot.root, "beancount-projection-"
    ) as root:
        _apply_overlays(root, overlays)
        projected_snapshot = dataclasses.replace(value.snapshot, root=root)
        projected = await asyncio.to_thread(load_snapshot, projected_snapshot)
        return success(
            [
                serialize_error(error, projected.snapshot.root)
                for error in projected.fava.errors
            ]
        )


@router.post("/source-files")
async def projected_source_files(
    body: dict[str, Any],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    overlays = body.get("files") if isinstance(body, dict) else []
    with cloned_workspace(
        value.snapshot.root, "beancount-source-files-"
    ) as root:
        _apply_overlays(root, overlays if overlays is not None else [])
        projected_snapshot = dataclasses.replace(value.snapshot, root=root)
        projected = await asyncio.to_thread(load_snapshot, projected_snapshot)
        return success(source_files(projected))


@router.post("/entry-ids")
async def projected_entry_ids(
    body: dict[str, Any],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    overlays = body.get("files") if isinstance(body, dict) else []
    locations = body.get("locations") if isinstance(body, dict) else []
    if not isinstance(locations, list) or len(locations) > MAX_LOCATIONS:
        raise ServiceError(
            400, f"at most {MAX_LOCATIONS} locations may be resolved at once"
        )
    parsed_locations: list[tuple[str, int]] = []
    for index, location in enumerate(locations):
        if (
            not isinstance(location, dict)
            or not isinstance(location.get("path"), str)
            or not isinstance(location.get("line"), int)
            or isinstance(location.get("line"), bool)
            or location["line"] < 1
        ):
            raise ServiceError(
                400, f"locations[{index}] must name a path and a 1-based line"
            )
        safe_repo_file_path(location["path"], f"locations[{index}].path")
        parsed_locations.append((location["path"], location["line"]))

    with cloned_workspace(
        value.snapshot.root, "beancount-entry-ids-"
    ) as root:
        _apply_overlays(root, overlays if overlays is not None else [])
        projected_snapshot = dataclasses.replace(value.snapshot, root=root)
        projected = await asyncio.to_thread(load_snapshot, projected_snapshot)
        ids = entry_ids_for_entries(projected.entries)
        by_location: dict[tuple[str, int], str] = {}
        resolved_root = root.resolve()
        for entry in projected.entries:
            meta = entry.meta or {}
            filename = meta.get("filename")
            lineno = meta.get("lineno")
            if not isinstance(filename, str) or not isinstance(lineno, int):
                continue
            try:
                rel = Path(filename).resolve().relative_to(resolved_root).as_posix()
            except (OSError, ValueError):
                continue
            by_location.setdefault((rel, lineno), ids[id(entry)])
        resolved_ids = [by_location.get(loc) for loc in parsed_locations]
        return success({"entry_hashes": resolved_ids})


@router.get("/plugins")
async def plugins(value: Annotated[LoadedLedger, Depends(loaded)]) -> dict[str, Any]:
    return success(plugin_names(value))


@router.get("/options")
async def options(value: Annotated[LoadedLedger, Depends(loaded)]) -> dict[str, Any]:
    return success(beancount_options(value))


@router.get("/fava-options")
async def fava(value: Annotated[LoadedLedger, Depends(loaded)]) -> dict[str, Any]:
    return success(fava_options(value))


@router.get("/beancountio-options")
async def beancountio(value: Annotated[LoadedLedger, Depends(loaded)]) -> dict[str, Any]:
    return success(bcio_options(value))


@router.get("/attributes")
async def ledger_attributes(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    return success(attributes(value))


@router.get("/accounts")
async def ledger_accounts(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    return success(accounts(value))


@router.get("/source-files")
async def ledger_source_files(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    return success(source_files(value))


@router.get("/entries_count_per_type")
async def ledger_entries_count(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    return success(entries_count(list(filtered.entries)))


@router.get("/commodities")
async def commodities(
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    operating = list(value.options.get("operating_currency") or ["USD"])
    data = []
    for base, quote in value.fava.prices.commodity_pairs(operating):
        prices = value.fava.prices.get_all_prices((base, quote)) or []
        data.append(
            {
                "base": base,
                "quote": quote,
                "prices": [
                    {"date": price_date, "value": price_value}
                    for price_date, price_value in prices
                ],
            }
        )
    return success(json_value(data, value.snapshot.root))


@router.get("/events")
async def events(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    data = [
        {"date": entry.date, "type": entry.type, "description": entry.description}
        for entry in filtered.entries
        if isinstance(entry, Event)
    ]
    return success(json_value(data, value.snapshot.root))


def _public_meta(meta: dict[str, Any], value: LoadedLedger) -> dict[str, Any]:
    return {
        key: json_value(item, value.snapshot.root)
        for key, item in meta.items()
        if key not in {"filename", "lineno"}
        and not (key.startswith("__") and key.endswith("__"))
    }


@router.get("/documents")
async def documents(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    data = [
        {
            "date": entry.date,
            "account": entry.account,
            "filename": relative_filename(entry.filename, value.snapshot.root),
            "tags": sorted(entry.tags),
            "links": sorted(entry.links),
            "meta": _public_meta(entry.meta, value),
        }
        for entry in filtered.entries
        if isinstance(entry, Document)
    ]
    return success(json_value(data, value.snapshot.root))


def _transaction(value: LoadedLedger, entry: Transaction | None) -> Any:
    if entry is None:
        return None
    return json_value(
        {
            "date": entry.date,
            "payee": entry.payee,
            "narration": entry.narration,
            "postings": [
                {
                    "account": posting.account,
                    "amount": posting.units.number if posting.units else "",
                    "commodity": posting.units.currency if posting.units else "",
                    "price": posting.price.number if posting.price else None,
                }
                for posting in entry.postings
            ],
        },
        value.snapshot.root,
    )


@router.get("/payee-transactions")
async def payee_transaction(
    value: Annotated[LoadedLedger, Depends(loaded)],
    payee: str,
) -> dict[str, Any]:
    return success(_transaction(value, value.fava.attributes.payee_transaction(payee)))


@router.get("/narration-transactions")
async def narration_transaction(
    value: Annotated[LoadedLedger, Depends(loaded)],
    narration: str,
) -> dict[str, Any]:
    return success(
        _transaction(value, value.fava.attributes.narration_transaction(narration))
    )


@router.get("/payee-accounts")
async def payee_accounts(
    value: Annotated[LoadedLedger, Depends(loaded)],
    payee: str,
) -> dict[str, Any]:
    return success(list(value.fava.attributes.payee_accounts(payee)))


@router.get("/account_last_entries")
async def account_last_entries(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    tree = Tree(filtered.entries)
    account_data = value.fava.accounts
    roots = (
        value.options.get("name_assets", "Assets"),
        value.options.get("name_liabilities", "Liabilities"),
    )
    data = []
    for name in value.fava.attributes.accounts:
        if not any(name == root or name.startswith(f"{root}:") for root in roots):
            continue
        last_entry = account_data[name].last_entry
        if last_entry is None:
            continue
        data.append(
            {
                "account": name,
                "date": last_entry.date,
                "balance": units(tree.get(name).balance),
            }
        )
    return success(json_value(data, value.snapshot.root))


@router.get("/postings_per_account")
async def postings_per_account(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    counts: dict[str, int] = {}
    for entry in filtered.entries:
        if not isinstance(entry, Transaction):
            continue
        for posting in entry.postings:
            counts[posting.account] = counts.get(posting.account, 0) + 1
    return success(
        [
            {"account": name, "count": count}
            for name, count in sorted(counts.items())
        ]
    )


for path, attribute in (
    ("narrations", "narrations"),
    ("payees", "payees"),
    ("links", "links"),
    ("years", "years"),
    ("currencies", "currencies"),
    ("tags", "tags"),
):
    async def collection(
        value: Annotated[LoadedLedger, Depends(loaded)],
        attribute_name: str = attribute,
    ) -> dict[str, Any]:
        return success(list(getattr(value.fava.attributes, attribute_name)))

    router.add_api_route(f"/{path}", collection, methods=["GET"])


def _conversion(value: LoadedLedger, conversion: str | None) -> str:
    currencies = value.options.get("operating_currency", [])
    return conversion or (currencies[0] if currencies else "USD")


def _filtered(
    value: LoadedLedger,
    account: str | None,
    filter: str | None,
    time: str | None,
):
    return value.fava.get_filtered(account=account, filter=filter, time=time)


@router.get("/hierarchy")
async def hierarchy(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account_name: str,
    conversion: str | None = None,
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = ChartModule().hierarchy(
        _filtered(value, account, filter, time),
        account_name,
        _conversion(value, conversion),
    )
    return success(json_value(data, value.snapshot.root))


@router.get("/interval-totals")
async def interval_totals(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account_name: str,
    conversion: str | None = None,
    interval: str = "month",
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = ChartModule().interval_totals(
        _filtered(value, account, filter, time),
        INTERVALS[interval],
        account_name,
        _conversion(value, conversion),
    )
    return success(json_value(data, value.snapshot.root))


@router.get("/account_report")
async def account_report(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account_name: str,
    conversion: str | None = None,
    interval: str = "month",
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = _filtered(value, account, filter, time)
    report_conversion = _conversion(value, conversion)
    chart = ChartModule()
    totals = chart.interval_totals(
        filtered,
        INTERVALS[interval],
        account_name,
        report_conversion,
    )
    data = {
        "linechart_data": chart.linechart(filtered, account_name, report_conversion),
        "account_balance_data": chart.account_balance(
            filtered,
            INTERVALS[interval],
            account_name,
            report_conversion,
        ),
        "interval_totals_data": [
            {"date": item.date, "balance": item.balance} for item in totals
        ],
    }
    return success(json_value(data, value.snapshot.root))


@router.get("/overview")
async def overview(
    value: Annotated[LoadedLedger, Depends(loaded)],
    conversion: str | None = None,
    interval: str = "month",
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = FinancialStatementsModule().overview(
        _filtered(value, account, filter, time),
        INTERVALS[interval],
        _conversion(value, conversion),
    )
    response = {
        "net_worth_data": data.net_worth_data,
        "assets_data": data.assets_data,
        "assets_hierarchy_data": data.assets_hierarchy,
        "liabilities_data": data.liabilities_data,
        "liabilities_hierarchy_data": data.liabilities_hierarchy,
        "income_data": data.income_data,
        "income_interval_data": data.income_interval_data,
        "income_hierarchy_data": data.income_hierarchy,
        "expenses_data": data.expenses_data,
        "expenses_interval_data": data.expenses_interval_data,
        "expenses_hierarchy_data": data.expenses_hierarchy,
    }
    return success(json_value(response, value.snapshot.root))


@router.get("/income-statement")
async def income_statement(
    value: Annotated[LoadedLedger, Depends(loaded)],
    conversion: str | None = None,
    interval: str = "month",
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = FinancialStatementsModule().income_statement(
        _filtered(value, account, filter, time),
        INTERVALS[interval],
        _conversion(value, conversion),
    )
    response = {
        "net_profit_data": [
            {"date": item.date, "balance": item.balance}
            for item in data.net_profit_data
        ],
        "income_data": data.income_data,
        "expenses_data": data.expenses_data,
        "income_hierarchy_data": data.income_hierarchy,
        "expenses_hierarchy_data": data.expenses_hierarchy,
    }
    return success(json_value(response, value.snapshot.root))


@router.get("/balance-sheet")
async def balance_sheet(
    value: Annotated[LoadedLedger, Depends(loaded)],
    conversion: str | None = None,
    interval: str = "month",
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = FinancialStatementsModule().balance_sheet(
        _filtered(value, account, filter, time),
        INTERVALS[interval],
        _conversion(value, conversion),
    )
    response = {
        "net_worth_data": data.net_worth_data,
        "assets_data": data.assets_data,
        "liabilities_data": data.liabilities_data,
        "equity_data": data.equity_data,
        "assets_hierarchy_data": data.assets_hierarchy,
        "liabilities_hierarchy_data": data.liabilities_hierarchy,
        "equity_hierarchy_data": data.equity_hierarchy,
    }
    return success(json_value(response, value.snapshot.root))


@router.get("/trial-balance")
async def trial_balance(
    value: Annotated[LoadedLedger, Depends(loaded)],
    conversion: str | None = None,
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    data = FinancialStatementsModule().trial_balance(
        _filtered(value, account, filter, time),
        _conversion(value, conversion),
    )
    response = {
        "income_hierarchy_data": data.income_hierarchy,
        "liabilities_hierarchy_data": data.liabilities_hierarchy,
        "equity_hierarchy_data": data.equity_hierarchy,
        "expenses_hierarchy_data": data.expenses_hierarchy,
        "assets_hierarchy_data": data.assets_hierarchy,
        "valuation_adjustment": data.valuation_adjustment,
        "rounding_residual": data.rounding_residual,
    }
    return success(json_value(response, value.snapshot.root))

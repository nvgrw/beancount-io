from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from beancount.core import data as bc_data
from beancount.core.amount import Amount
from fastapi import APIRouter, Depends
from fava.beans import create
from fava.file import format_entries

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import GiteaClient, repo_path, safe_repo_file_path
from .journal import entry_ids_for_entries
from .ledger import (
    LoadedLedger,
    add_includes,
    commit_sha_of,
    count_ledger_errors,
    load_snapshot,
    project_error_summary,
    refuse_projected_errors,
    serialize_error,
)
from .main_support import load_ledger
from .repository import _bare_repo
from .serializers import source_files
from .workspace import cloned_workspace, write_text_copy_on_write


router = APIRouter(prefix="/entries/{owner}/{repo}")
DEFAULT_INFERRED_TOLERANCE = Decimal("0.005")


async def load_optional_ledger(
    owner: str,
    repo: str,
    auth: RequestAuth,
) -> LoadedLedger | None:
    try:
        _bare_repo(owner, repo)
    except ServiceError as error:
        if error.status == 404:
            return None
        raise
    return await load_ledger(owner, repo, auth)


def _date(value: Any) -> datetime.date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO date")
    return datetime.date.fromisoformat(value)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _amount(value: Any) -> Amount:
    if not isinstance(value, dict):
        raise ValueError("amount must be an object")
    return create.amount(_decimal(value["number"]), str(value["currency"]))


def _meta(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("meta must be an object")
    return dict(value)


def _posting(value: Any, transaction_date: datetime.date):
    if not isinstance(value, dict):
        raise ValueError("posting must be an object")
    cost = value.get("cost")
    built_cost = None
    if cost is not None:
        if not isinstance(cost, dict):
            raise ValueError("posting cost must be an object")
        built_cost = create.cost(
            _decimal(cost["number"]),
            str(cost["currency"]),
            _date(cost.get("date") or transaction_date.isoformat()),
            cost.get("label"),
        )
    units = value.get("units")
    price = value.get("price")
    total_price = value.get("totalPrice")
    meta = _meta(value.get("meta"))
    if total_price is not None and price is None:
        # Mark total price so post-format replacement emits `@@ <num> <cur>`.
        if not isinstance(total_price, dict):
            raise ValueError("posting totalPrice must be an object")
        _decimal(total_price["number"])
        str(total_price["currency"])
    built_price = _amount(price) if price is not None else None
    return create.posting(
        str(value["account"]),
        _amount(units) if units is not None else None,
        built_cost,
        built_price,
        value.get("flag"),
        meta or None,
    )


def _custom_value(value: Any):
    if not isinstance(value, dict):
        raise ValueError("custom value must be an object")
    kind = value.get("kind")
    if kind == "text":
        return create.custom_value_text(str(value["value"]))
    if kind == "number":
        return create.custom_value_number(_decimal(value["value"]))
    if kind == "amount":
        return create.custom_value_amount(
            _decimal(value["number"]), str(value["currency"])
        )
    if kind == "account":
        return create.custom_value_account(str(value["value"]))
    raise ValueError(f"unsupported custom value kind: {kind}")


def _implied_tolerance(number: Any) -> Decimal:
    value = Decimal(str(number))
    return Decimal("0.5").scaleb(value.as_tuple().exponent)


def _check_transaction(item: dict[str, Any], index: int) -> None:
    postings = item.get("postings")
    if not isinstance(postings, list):
        return
    omitted = [posting for posting in postings if posting.get("units") is None]
    if len(omitted) > 1:
        raise ServiceError(
            400,
            f"entry {index}: at most one posting may omit its amount, found {len(omitted)}",
        )
    sums: dict[str, Decimal] = {}
    tolerances: dict[str, Decimal] = {}
    for posting in postings:
        units = posting.get("units")
        if units is None:
            continue
        if not isinstance(units, dict):
            raise ServiceError(400, f"entry {index}: posting units must be an object")
        try:
            number = Decimal(str(units["number"]))
            currency = str(units["currency"])
        except (KeyError, ValueError) as exc:
            raise ServiceError(400, f"entry {index}: invalid posting amount") from exc
        if not number.is_finite():
            raise ServiceError(400, f"entry {index}: posting amount is not finite")
        # If posting carries a price/cost/totalPrice, weight is in the price/cost currency.
        cost = posting.get("cost")
        price = posting.get("price")
        total_price = posting.get("totalPrice")
        if isinstance(cost, dict) and "number" in cost and "currency" in cost:
            weight_num = number * _decimal(cost["number"])
            weight_cur = str(cost["currency"])
            tol = _implied_tolerance(cost["number"])
        elif isinstance(total_price, dict) and "number" in total_price and "currency" in total_price:
            sign = Decimal("-1") if number < 0 else Decimal("1")
            weight_num = sign * abs(_decimal(total_price["number"]))
            weight_cur = str(total_price["currency"])
            tol = _implied_tolerance(total_price["number"])
        elif isinstance(price, dict) and "number" in price and "currency" in price:
            weight_num = number * _decimal(price["number"])
            weight_cur = str(price["currency"])
            tol = _implied_tolerance(price["number"])
        else:
            weight_num = number
            weight_cur = currency
            tol = _implied_tolerance(units["number"])
        sums[weight_cur] = sums.get(weight_cur, Decimal()) + weight_num
        tolerances[weight_cur] = max(tolerances.get(weight_cur, Decimal()), tol)
    residuals = {currency: amount for currency, amount in sums.items() if amount}
    if omitted:
        if len(residuals) != 1:
            raise ServiceError(
                400,
                f"entry {index}: cannot interpolate one omitted amount across {len(residuals)} residual currencies",
            )
        return
    offending = {
        currency: amount
        for currency, amount in residuals.items()
        if abs(amount) > max(DEFAULT_INFERRED_TOLERANCE, tolerances[currency])
    }
    if offending:
        rendered = ", ".join(
            f"{abs(amount)} {currency}" for currency, amount in offending.items()
        )
        raise ServiceError(
            400,
            f"entry {index}: Transaction does not balance: residual {rendered}",
            "UNBALANCED_TRANSACTION",
            {"residual": rendered, "entry": index},
        )


def _booking(value: Any) -> bc_data.Booking | None:
    if value is None or value == "":
        return None
    name = str(value).strip().upper()
    try:
        return bc_data.Booking[name]
    except KeyError as exc:
        raise ValueError(f"unsupported booking method: {value}") from exc


def _directive(row: dict[str, Any]):
    kind = row.get("type")
    item = row.get("item")
    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    date = _date(item.get("date"))
    meta = _meta(item.get("meta"))
    if kind == "transaction":
        return create.transaction(
            meta,
            date,
            str(item.get("flag") or "*"),
            item.get("payee"),
            item.get("narration"),
            frozenset(item.get("tags") or []),
            frozenset(item.get("links") or []),
            [_posting(posting, date) for posting in item.get("postings") or []],
        )
    if kind == "balance":
        tol = item.get("tolerance")
        return create.balance(
            meta,
            date,
            str(item["account"]),
            _amount(item["amount"]),
            _decimal(tol) if tol is not None else None,
        )
    if kind == "pad":
        return bc_data.Pad(
            meta,
            date,
            str(item["account"]),
            str(item["source_account"]),
        )
    if kind == "commodity":
        return create.commodity(meta, date, str(item["currency"]))
    if kind == "price":
        return create.price(meta, date, str(item["currency"]), _amount(item["amount"]))
    if kind == "note":
        return create.note(meta, date, str(item["account"]), str(item["comment"]))
    if kind == "open":
        return create.open(
            meta,
            date,
            str(item["account"]),
            list(item.get("currencies") or []),
            _booking(item.get("booking")),
        )
    if kind == "close":
        return create.close(meta, date, str(item["account"]))
    if kind == "event":
        return create.event(meta, date, str(item["type"]), str(item["description"]))
    if kind == "custom":
        return create.custom(
            meta,
            date,
            str(item["type"]),
            [_custom_value(value) for value in item.get("values") or []],
        )
    if kind == "document":
        return create.document(
            meta,
            date,
            str(item["account"]),
            str(item["filename"]),
            frozenset(item.get("tags") or []),
            frozenset(item.get("links") or []),
        )
    raise ValueError(f"unsupported entry type: {kind}")


def _format_row(row: dict[str, Any], directive: Any) -> str:
    text = format_entries([directive])
    if row.get("type") != "transaction":
        return text
    postings = (row.get("item") or {}).get("postings") or []
    extras = [
        (
            posting.get("totalPrice") if posting.get("price") is None else None,
            posting.get("tags") or [],
            posting.get("links") or [],
        )
        for posting in postings
        if isinstance(posting, dict)
    ]
    if not any(tp or tags or links for tp, tags, links in extras):
        return text
    lines = text.splitlines(keepends=True)
    posting_idx = 0
    for line_idx in range(1, len(lines)):
        stripped = lines[line_idx].lstrip()
        if not stripped or stripped.startswith(";") or ":" not in stripped or stripped.split()[0].endswith(":"):
            continue
        if posting_idx < len(extras):
            tp, tags, links = extras[posting_idx]
            posting_idx += 1
            suffix_parts: list[str] = []
            if isinstance(tp, dict):
                suffix_parts.append(f"@@ {tp['number']} {tp['currency']}")
            for tag in tags:
                suffix_parts.append(f"#{tag}")
            for link in links:
                suffix_parts.append(f"^{link}")
            if suffix_parts:
                eol = "\r\n" if lines[line_idx].endswith("\r\n") else "\n"
                lines[line_idx] = (
                    lines[line_idx].rstrip("\r\n") + " " + " ".join(suffix_parts) + eol
                )
    return "".join(lines)


def _project_bulk(
    value: LoadedLedger,
    updates: dict[str, str],
    locations: list[tuple[str, int]],
    allow_invalid: bool,
) -> tuple[dict[str, Any], list[str]]:
    before_errors = [
        serialize_error(item, value.snapshot.root) for item in value.fava.errors
    ]
    with cloned_workspace(
        value.snapshot.root, "beancount-bulk-entries-"
    ) as root:
        for path, content in updates.items():
            write_text_copy_on_write(root / path, content)
        projected = load_snapshot(dataclasses.replace(value.snapshot, root=root))
        after_errors = [
            serialize_error(item, projected.snapshot.root)
            for item in projected.fava.errors
        ]
        summary = project_error_summary(before_errors, after_errors)
        if not allow_invalid:
            refuse_projected_errors(summary, "add")

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
        resolved = [by_location.get(loc) for loc in locations]
        hashes = [h for h in resolved if h is not None]
        return summary, hashes if len(hashes) == len(locations) else []


@router.post("/bulk")
async def add_bulk_entries(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    rows = body.get("entries")
    if not isinstance(rows, list):
        raise ServiceError(400, "entries must be an array")
    default_filename = body.get("filename") or "main.bean"
    allow_invalid = body.get("allowInvalid") is True
    dry_run = body.get("dryRun") is True

    by_file: dict[str, list[tuple[int, str]]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ServiceError(400, f"entry {index}: entry must be an object")
        target = row.get("filename") or default_filename
        safe_repo_file_path(target, f"entries[{index}].filename")
        if row.get("type") == "transaction" and not allow_invalid:
            item = row.get("item")
            if isinstance(item, dict):
                _check_transaction(item, index)
        try:
            directive = _directive(row)
            formatted = _format_row(row, directive)
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError(400, f"entry {index}: {exc}") from exc
        by_file.setdefault(str(target), []).append((index, formatted))

    value = await load_optional_ledger(owner, repo, auth)

    if not by_file:
        current_count = (
            count_ledger_errors(
                [serialize_error(e, value.snapshot.root) for e in value.fava.errors]
            )
            if value is not None
            else 0
        )
        return success(
            {
                "entry_hashes": [],
                "included_files": [],
                "errors_before": current_count,
                "errors_after": current_count,
                "new_errors": [],
            }
        )

    client = GiteaClient(auth)
    current_files: dict[str, tuple[str, str]] = {}

    async def read_gitea_file(path: str) -> tuple[str, str]:
        try:
            raw = await client.request(
                "GET", repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}")
            )
        except ServiceError as error:
            if error.status == 404 and dry_run:
                return "", ""
            raise
        if not isinstance(raw, dict) or not isinstance(raw.get("sha"), str):
            raise ServiceError(404, f"File {path} not found")
        try:
            decoded = base64.b64decode(str(raw.get("content") or "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError(502, f"Gitea returned invalid content for {path}") from exc
        return decoded, raw["sha"]

    for path in by_file:
        current_files[path] = await read_gitea_file(path)

    included_files: list[str] = []
    entrypoint = value.snapshot.entrypoint if value is not None else "main.bean"
    if value is not None:
        loaded_set = set(source_files(value))
        included_files = [path for path in by_file if path not in loaded_set]
        if included_files:
            if entrypoint not in current_files:
                current_files[entrypoint] = await read_gitea_file(entrypoint)
            entry_text, entry_sha = current_files[entrypoint]
            current_files[entrypoint] = (
                add_includes(entry_text, included_files),
                entry_sha,
            )

    locations: list[tuple[str, int] | None] = [None] * len(rows)
    preview: list[dict[str, str]] = []
    projected_updates: dict[str, str] = {}
    operations: list[dict[str, Any]] = []

    for path, entries_for_file in by_file.items():
        current, blob_sha = current_files[path]
        eol = "\r\n" if "\r\n" in current else "\n"
        updated = current + eol
        appended = ""
        for idx, block in entries_for_file:
            normalized_block = block.replace("\r\n", "\n").replace("\n", eol)
            start_line = updated.count("\n") + 1
            locations[idx] = (path, start_line)
            updated += normalized_block
            appended += normalized_block
        preview.append({"path": path, "text": appended})
        projected_updates[path] = updated
        operations.append(
            {
                "operation": "update",
                "path": path,
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
            }
        )

    if included_files and entrypoint not in by_file and entrypoint in current_files:
        entry_text, entry_sha = current_files[entrypoint]
        projected_updates[entrypoint] = entry_text
        operations.append(
            {
                "operation": "update",
                "path": entrypoint,
                "content": base64.b64encode(entry_text.encode("utf-8")).decode("ascii"),
                "sha": entry_sha,
            }
        )

    resolved_locations = [loc for loc in locations if loc is not None]
    if value is not None:
        summary, entry_hashes = await asyncio.to_thread(
            _project_bulk,
            value,
            projected_updates,
            resolved_locations,
            allow_invalid,
        )
    else:
        summary = {"errors_before": 0, "errors_after": 0, "new_errors": []}
        entry_hashes = []

    if dry_run:
        return success(
            {
                "entry_hashes": [],
                "included_files": included_files,
                "preview": preview,
                **summary,
            }
        )

    commit_sha: str | None = None
    if operations:
        resp = await client.request(
            "POST",
            repo_path(owner, repo, "/contents"),
            json={
                "files": operations,
                "message": f"Add {len(rows)} ledger {'entry' if len(rows) == 1 else 'entries'}",
            },
        )
        commit_sha = commit_sha_of(resp)

    result: dict[str, Any] = {
        "entry_hashes": entry_hashes,
        "included_files": included_files,
        **summary,
    }
    if commit_sha:
        result["commit"] = commit_sha
    return success(result)

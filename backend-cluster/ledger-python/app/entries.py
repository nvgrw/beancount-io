from __future__ import annotations

import base64
import datetime
from decimal import Decimal
from typing import Annotated, Any

from beancount.core.amount import Amount
from fastapi import APIRouter, Depends
from fava.beans import create
from fava.file import format_entries

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import GiteaClient, repo_path, safe_repo_file_path


router = APIRouter(prefix="/entries/{owner}/{repo}")
DEFAULT_INFERRED_TOLERANCE = Decimal("0.005")


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
    return create.posting(
        str(value["account"]),
        _amount(units) if units is not None else None,
        built_cost,
        _amount(price) if price is not None else None,
        value.get("flag"),
        _meta(value.get("meta")) or None,
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
        sums[currency] = sums.get(currency, Decimal()) + number
        tolerances[currency] = max(
            tolerances.get(currency, Decimal()),
            _implied_tolerance(units["number"]),
        )
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
            400, f"entry {index}: Transaction does not balance: residual {rendered}"
        )


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
        return create.balance(meta, date, str(item["account"]), _amount(item["amount"]))
    if kind == "commodity":
        return create.commodity(meta, date, str(item["currency"]))
    if kind == "price":
        return create.price(meta, date, str(item["currency"]), _amount(item["amount"]))
    if kind == "note":
        return create.note(meta, date, str(item["account"]), str(item["comment"]))
    if kind == "open":
        return create.open(meta, date, str(item["account"]), list(item.get("currencies") or []))
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
    by_file: dict[str, list[Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ServiceError(400, f"entry {index}: entry must be an object")
        target = row.get("filename") or default_filename
        safe_repo_file_path(target, f"entries[{index}].filename")
        if row.get("type") == "transaction" and body.get("allowInvalid") is not True:
            item = row.get("item")
            if isinstance(item, dict):
                _check_transaction(item, index)
        try:
            directive = _directive(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError(400, f"entry {index}: {exc}") from exc
        by_file.setdefault(str(target), []).append(directive)

    client = GiteaClient(auth)
    operations = []
    for path, directives in by_file.items():
        value = await client.request(
            "GET", repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}")
        )
        if not isinstance(value, dict) or not isinstance(value.get("sha"), str):
            raise ServiceError(404, f"File {path} not found")
        try:
            current = base64.b64decode(str(value.get("content") or "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError(502, f"Gitea returned invalid content for {path}") from exc
        updated = current + "\n" + format_entries(directives)
        operations.append(
            {
                "operation": "update",
                "path": path,
                "content": base64.b64encode(updated.encode()).decode(),
                "sha": value["sha"],
            }
        )
    if operations:
        await client.request(
            "POST",
            repo_path(owner, repo, "/contents"),
            json={
                "files": operations,
                "message": f"Add {len(rows)} ledger {'entry' if len(rows) == 1 else 'entries'}",
            },
        )
    return success(None)

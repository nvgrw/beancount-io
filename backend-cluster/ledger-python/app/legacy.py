from __future__ import annotations

import re
from typing import Annotated, Any

from beancount.core.data import Transaction
from fastapi import APIRouter, Depends
from fava.beans.funcs import hash_entry
from fava.serialisation import serialise

from .auth import RequestAuth, request_auth
from .errors import success
from .ledger import LoadedLedger
from .main_support import load_ledger
from .serializers import json_value


router = APIRouter(prefix="/legacy/journal/{owner}/{repo}")


async def loaded(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> LoadedLedger:
    return await load_ledger(owner, repo, auth)


def _max_amount(entry: Any) -> float:
    if not isinstance(entry, Transaction):
        return 0
    return max(
        (abs(float(posting.units.number)) for posting in entry.postings if posting.units),
        default=0,
    )


@router.get("")
async def legacy_journal(
    value: Annotated[LoadedLedger, Depends(loaded)],
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
    search_query: str | None = None,
    account_filter: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    entry_types: str | None = None,
    sort_by: str = "date",
    sort_order: str = "desc",
    detailed: bool = False,
) -> dict[str, Any]:
    entries = list(value.entries)
    if after and re.fullmatch(r"\d{4}-\d{2}-\d{2}", after):
        entries = [entry for entry in entries if entry.date.isoformat() > after]
    if before and re.fullmatch(r"\d{4}-\d{2}-\d{2}", before):
        entries = [entry for entry in entries if entry.date.isoformat() < before]
    allowed = {item.strip() for item in (entry_types or "").split(",") if item.strip()}
    pattern = None
    if account_filter:
        try:
            pattern = re.compile(account_filter, re.IGNORECASE)
        except re.error:
            pattern = None

    def include(entry: Any) -> bool:
        if allowed and type(entry).__name__ not in allowed:
            return False
        if search_query:
            needle = search_query.lower()
            if not isinstance(entry, Transaction):
                return False
            text = " ".join(
                [entry.payee or "", entry.narration or "", *(posting.account for posting in entry.postings)]
            ).lower()
            if needle not in text:
                return False
        accounts = [posting.account for posting in entry.postings] if isinstance(entry, Transaction) else [getattr(entry, "account", "")]
        if account_filter and not any(
            pattern.search(account) if pattern else account_filter.lower() in account.lower()
            for account in accounts
        ):
            return False
        if isinstance(entry, Transaction) and (amount_min is not None or amount_max is not None):
            amounts = [abs(float(posting.units.number)) for posting in entry.postings if posting.units]
            if not any(
                (amount_min is None or amount >= amount_min)
                and (amount_max is None or amount <= amount_max)
                for amount in amounts
            ):
                return False
        return True

    entries = [entry for entry in entries if include(entry)]
    key = (
        (lambda entry: entry.date)
        if sort_by == "date"
        else (lambda entry: entry.payee or "")
        if sort_by == "payee"
        else _max_amount
    )
    entries.sort(key=key, reverse=sort_order.lower() == "desc")
    if after and after.isdigit():
        entries = entries[int(after) + 1 :]
    if before and before.isdigit():
        entries = entries[: int(before)]
    if first is not None:
        entries = entries[:first]
    if last is not None:
        entries = entries[-last:]
    result = []
    for entry in entries:
        item = json_value(serialise(entry), value.snapshot.root)
        if detailed:
            item["entry_hash"] = hash_entry(entry)
            item["entry_type"] = type(entry).__name__.lower()
            meta = item.get("meta")
            if isinstance(meta, dict):
                for key_name in ("__tolerances__", "__automatic__", "filename", "lineno"):
                    meta.pop(key_name, None)
        result.append(item)
    return success(result)

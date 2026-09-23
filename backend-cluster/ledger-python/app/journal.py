from __future__ import annotations

import datetime
import asyncio
import base64
import binascii
import dataclasses
import hashlib
import re
import shutil
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from beancount.core.amount import Amount
from beancount.core.inventory import Inventory
from beancount.core.data import (
    Balance,
    Close,
    Commodity,
    Custom,
    Document,
    Event,
    Note,
    Open,
    Pad,
    Price,
    Query as QueryDirective,
    Transaction,
)
from beancount.core.getters import get_entry_accounts
from fastapi import APIRouter, Depends, Query
from fava.beans.funcs import hash_entry
from fava.beans.str import to_string

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import GiteaClient, repo_path, safe_repo_file_path
from .ledger import LoadedLedger, load_snapshot
from .main_support import load_ledger
from .serializers import json_value, relative_filename


router = APIRouter(prefix="/journal/{owner}/{repo}")
PLAINTEXT_EXCLUDED_FLAGS = {"P", "S", "T", "C", "U", "R", "M"}


async def loaded(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> LoadedLedger:
    return await load_ledger(owner, repo, auth)


def _entry_ids(entries: list[Any]) -> dict[int, str]:
    counts: defaultdict[str, int] = defaultdict(int)
    result: dict[int, str] = {}
    for entry in entries:
        base = hash_entry(entry)
        occurrence = counts[base]
        counts[base] += 1
        result[id(entry)] = base if occurrence == 0 else f"{base}:{occurrence}"
    return result


def _resolve_entry(value: LoadedLedger, entry_id: str) -> Any:
    base = entry_id
    occurrence = 0
    match = re.fullmatch(r"(.+):(\d+)", entry_id)
    if match is not None:
        base = match.group(1)
        occurrence = int(match.group(2))
    matches = [entry for entry in value.entries if hash_entry(entry) == base]
    if occurrence >= len(matches):
        raise ServiceError(404, f"Entry not found: {entry_id}")
    return matches[occurrence]


def _source_slice(value: LoadedLedger, entry: Any) -> tuple[str, str]:
    meta = entry.meta or {}
    filename = meta.get("filename")
    lineno = meta.get("lineno")
    if not isinstance(filename, str) or not isinstance(lineno, int) or lineno < 1:
        raise ServiceError(404, "Entry source is unavailable")
    source = Path(filename).resolve()
    try:
        source.relative_to(value.snapshot.root.resolve())
    except ValueError as exc:
        raise ServiceError(404, "Entry source is outside the repository") from exc
    try:
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        raise ServiceError(404, "Entry source is unavailable") from exc
    start = lineno - 1
    if start >= len(lines):
        raise ServiceError(404, "Entry source line is unavailable")
    end = start + 1
    while end < len(lines) and lines[end].strip() and lines[end].startswith((" ", "\t")):
        end += 1
    source_slice = "".join(lines[start:end]).rstrip("\r\n")
    digest = hashlib.sha256(source_slice.encode("utf-8")).hexdigest()
    return source_slice, digest


def _source_path(value: LoadedLedger, entry: Any) -> tuple[str, int]:
    meta = entry.meta or {}
    filename = meta.get("filename")
    lineno = meta.get("lineno")
    if not isinstance(filename, str) or not isinstance(lineno, int) or lineno < 1:
        raise ServiceError(404, "Entry source is unavailable")
    try:
        path = str(Path(filename).resolve().relative_to(value.snapshot.root.resolve()))
    except ValueError as exc:
        raise ServiceError(404, "Entry source is outside the repository") from exc
    safe_repo_file_path(path)
    return path, lineno - 1


def _blocks(content: str) -> list[tuple[int, int, str, str]]:
    lines = content.splitlines(keepends=True)
    result: list[tuple[int, int, str, str]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip() or lines[index].startswith((" ", "\t")):
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() and lines[end].startswith((" ", "\t")):
            end += 1
        source = "".join(lines[index:end]).rstrip("\r\n")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        result.append((index, end, source, digest))
        index = end
    return result


def _relocate_block(
    value: LoadedLedger,
    path: str,
    original_start: int,
    expected_sha: str,
    current: str,
) -> tuple[int, int, str, str]:
    original = (value.snapshot.root / path).read_text(encoding="utf-8")
    old_matches = [block for block in _blocks(original) if block[3] == expected_sha]
    old_index = next(
        (index for index, block in enumerate(old_matches) if block[0] == original_start),
        None,
    )
    if old_index is None:
        raise ServiceError(409, "Entry has changed since it was loaded; refresh and retry")
    current_matches = [block for block in _blocks(current) if block[3] == expected_sha]
    if old_index >= len(current_matches):
        raise ServiceError(409, "Entry has changed since it was loaded; refresh and retry")
    return current_matches[old_index]


def _replace_block(
    content: str,
    block: tuple[int, int, str, str],
    replacement: str | None,
) -> str:
    lines = content.splitlines(keepends=True)
    start, end = block[0], block[1]
    inserted: list[str] = []
    if replacement is not None:
        newline = "\r\n" if "\r\n" in content else "\n"
        inserted = [replacement.rstrip("\r\n") + newline]
    return "".join([*lines[:start], *inserted, *lines[end:]])


async def _gitea_file(
    client: GiteaClient,
    owner: str,
    repo: str,
    path: str,
) -> tuple[str, str]:
    value = await client.request(
        "GET", repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}")
    )
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str):
        raise ServiceError(502, "Gitea returned an invalid file response")
    encoded = value.get("content")
    if not isinstance(encoded, str):
        raise ServiceError(502, "Gitea returned a file without content")
    try:
        content = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ServiceError(502, "Gitea returned invalid file content") from exc
    return content, value["sha"]


def _projected_entry_id(
    value: LoadedLedger,
    path: str,
    content: str,
    lineno: int,
) -> str:
    with tempfile.TemporaryDirectory(prefix="beancount-source-edit-") as directory:
        root = Path(directory) / "repository"
        shutil.copytree(value.snapshot.root, root)
        target = root / path
        target.write_text(content, encoding="utf-8")
        projected = load_snapshot(dataclasses.replace(value.snapshot, root=root))
        ids = _entry_ids(projected.entries)
        for entry in projected.entries:
            meta = entry.meta or {}
            filename = meta.get("filename")
            if not isinstance(filename, str) or meta.get("lineno") != lineno:
                continue
            try:
                relative = str(Path(filename).resolve().relative_to(root.resolve()))
            except ValueError:
                continue
            if relative == path:
                return ids[id(entry)]
    raise ServiceError(400, "Updated content does not contain a valid entry")


def _entry_balances(
    value: LoadedLedger,
    entry: Any,
) -> tuple[dict[str, list[str]] | None, dict[str, list[str]] | None]:
    if not isinstance(entry, Balance | Transaction):
        return None, None
    balances = {account: Inventory() for account in get_entry_accounts(entry)}
    for current in value.entries:
        if current is entry:
            break
        if isinstance(current, Transaction):
            for posting in current.postings:
                balance = balances.get(posting.account)
                if balance is not None:
                    balance.add_position(posting)

    def visualise() -> dict[str, list[str]]:
        return {
            account: [to_string(position) for position in sorted(inventory)]
            for account, inventory in balances.items()
        }

    before = visualise()
    if isinstance(entry, Balance):
        return before, None
    for posting in entry.postings:
        balances[posting.account].add_position(posting)
    return before, visualise()


def _amount(value: Amount | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"number": str(value.number), "currency": value.currency}


def _meta(value: Any, root: Path) -> dict[str, Any] | None:
    if not value:
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.startswith("__") and key.endswith("__"):
            continue
        if key == "filename" and isinstance(item, str):
            result[key] = relative_filename(item, root)
        else:
            result[key] = json_value(item, root)
    return result or None


def _cost(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "number": str(value.number) if value.number is not None else "0",
        "currency": value.currency or "",
        "date": value.date.isoformat() if value.date else "",
        "label": value.label,
    }


def _posting(value: Any, root: Path) -> dict[str, Any]:
    return {
        "account": value.account,
        "units": _amount(value.units) or {"number": "", "currency": ""},
        "cost": _cost(value.cost),
        "price": _amount(value.price),
        "meta": _meta(value.meta, root),
        "flag": value.flag,
    }


def _custom_value(value: Any, root: Path) -> Any:
    raw = getattr(value, "value", value)
    if isinstance(raw, Amount):
        return _amount(raw)
    if isinstance(raw, Decimal | datetime.date):
        return str(raw)
    return json_value(raw, root)


def serialize_directive(
    entry: Any,
    root: Path,
    entry_hash: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entry_hash": entry_hash or hash_entry(entry),
        "directive_type": type(entry).__name__,
        "date": entry.date.isoformat(),
        "meta": _meta(entry.meta, root),
    }
    if isinstance(entry, Transaction):
        result.update(
            flag=entry.flag,
            payee=entry.payee,
            narration=entry.narration or "",
            postings=[_posting(posting, root) for posting in entry.postings],
            tags=sorted(entry.tags),
            links=sorted(entry.links),
        )
    elif isinstance(entry, Balance):
        result.update(account=entry.account, diff_amount=_amount(entry.diff_amount))
    elif isinstance(entry, Commodity):
        result["currency"] = entry.currency
    elif isinstance(entry, Close):
        result["account"] = entry.account
    elif isinstance(entry, Custom):
        result.update(
            type=entry.type,
            values=[_custom_value(item, root) for item in entry.values],
        )
    elif isinstance(entry, Document):
        result.update(
            filename=relative_filename(entry.filename, root),
            account=entry.account,
            tags=sorted(entry.tags),
            links=sorted(entry.links),
        )
    elif isinstance(entry, Event):
        result.update(type=entry.type, description=entry.description)
    elif isinstance(entry, Note):
        result.update(account=entry.account, comment=entry.comment)
    elif isinstance(entry, Open):
        booking = getattr(entry.booking, "value", entry.booking)
        result.update(
            account=entry.account,
            currencies=list(entry.currencies) or None,
            booking=booking,
        )
    elif isinstance(entry, Pad):
        result.update(account=entry.account, source_account=entry.source_account)
    elif isinstance(entry, Price):
        result.update(currency=entry.currency, amount=_amount(entry.amount))
    elif isinstance(entry, QueryDirective):
        result.update(type=entry.name, values=[entry.query_string])
        result["directive_type"] = "Custom"
    return result


def _matches_display_filters(
    item: dict[str, Any],
    directive_types: list[str] | None,
    transaction_subtypes: list[str] | None,
    document_subtypes: list[str] | None,
    custom_subtypes: list[str] | None,
) -> bool:
    directive_type = item["directive_type"]
    if directive_types and directive_type not in directive_types:
        return False
    if transaction_subtypes and directive_type == "Transaction":
        flag = item.get("flag")
        subtype = "cleared" if flag == "*" else "pending" if flag == "!" else "other"
        if subtype not in transaction_subtypes:
            return False
    if document_subtypes and directive_type == "Document":
        tags = item.get("tags") or []
        if not any(subtype in tags for subtype in document_subtypes):
            return False
    if custom_subtypes and directive_type == "Custom":
        if item.get("type") not in custom_subtypes:
            return False
    return True


@router.get("")
async def journal(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=0),
    directive_types: list[str] | None = Query(None),
    transaction_subtypes: list[str] | None = Query(None),
    document_subtypes: list[str] | None = Query(None),
    custom_subtypes: list[str] | None = Query(None),
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    entry_ids = _entry_ids(value.entries)
    items = [
        serialize_directive(
            entry,
            value.snapshot.root,
            entry_ids.get(id(entry), hash_entry(entry)),
        )
        for entry in reversed(filtered.entries)
        if not (isinstance(entry, Transaction) and entry.flag in {"C", "S"})
    ]
    items = [
        item
        for item in items
        if _matches_display_filters(
            item,
            directive_types,
            transaction_subtypes,
            document_subtypes,
            custom_subtypes,
        )
    ]
    return success(
        {
            "items": items[offset : offset + limit],
            "total": len(items),
            "is_empty": not value.entries,
        }
    )


@router.get("/account-journal")
async def account_journal(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str,
    filter_account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=0),
    with_children: bool = True,
    conversion: str = "at_cost",
    directive_types: list[str] | None = Query(None),
    transaction_subtypes: list[str] | None = Query(None),
    document_subtypes: list[str] | None = Query(None),
    custom_subtypes: list[str] | None = Query(None),
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(
        account=filter_account,
        filter=filter,
        time=time,
    )
    entry_ids = _entry_ids(value.entries)
    items = []
    for entry, change, balance in reversed(
        value.fava.account_journal(
            filtered,
            account,
            conversion,
            with_children=with_children,
        )
    ):
        serialized = serialize_directive(
            entry,
            value.snapshot.root,
            entry_ids.get(id(entry), hash_entry(entry)),
        )
        if _matches_display_filters(
            serialized,
            directive_types,
            transaction_subtypes,
            document_subtypes,
            custom_subtypes,
        ):
            items.append(
                {
                    "entry": serialized,
                    "change": json_value(change, value.snapshot.root),
                    "balance": json_value(balance, value.snapshot.root),
                }
            )
    return success(
        {
            "items": items[offset : offset + limit],
            "total": len(items),
            "account": account,
            "with_children": with_children,
        }
    )


@router.get("/plaintext")
async def plaintext_journal(
    value: Annotated[LoadedLedger, Depends(loaded)],
    account: str | None = None,
    filter: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    title = str(value.options.get("title") or "Journal Export")
    title = title.replace("\\", "\\\\").replace('"', '\\"')
    content = ";; -*- mode: org; mode: beancount; -*-\n\n"
    content += f'option "title" "{title} - Journal Export"\n\n'
    for currency in value.options.get("operating_currency", []):
        content += f'option "operating_currency" "{currency}"\n'
    content += f'\noption "name_assets" "{value.options["name_assets"]}"\n'
    content += f'option "name_liabilities" "{value.options["name_liabilities"]}"\n'
    content += f'option "name_equity" "{value.options["name_equity"]}"\n'
    content += f'option "name_income" "{value.options["name_income"]}"\n'
    content += f'option "name_expenses" "{value.options["name_expenses"]}"\n'
    content += 'plugin "beancount.plugins.auto_accounts"\n\n'
    for entry in filtered.entries:
        if not isinstance(entry, Balance | Transaction):
            continue
        if isinstance(entry, Transaction) and entry.flag in PLAINTEXT_EXCLUDED_FLAGS:
            continue
        content += f"{to_string(entry).rstrip(chr(10))}\n\n"
    return success({"content": content})


@router.get("/context/{entry_hash}")
async def context(
    entry_hash: str,
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    entry = _resolve_entry(value, entry_hash)
    source_slice, sha256sum = _source_slice(value, entry)
    balances_before, balances_after = _entry_balances(value, entry)
    return success(
        {
            "entry": serialize_directive(entry, value.snapshot.root, entry_hash),
            "balances_before": balances_before,
            "balances_after": balances_after,
            "sha256sum": sha256sum,
            "slice": source_slice,
        }
    )


@router.put("/source-slice")
async def update_source_slice(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    entry_id = str(body.get("entry_hash") or "")
    expected_sha = str(body.get("sha256sum") or "")
    new_content = body.get("new_content")
    if not isinstance(new_content, str) or not new_content.strip():
        raise ServiceError(400, "new_content must contain an entry")
    entry = _resolve_entry(value, entry_id)
    path, original_start = _source_path(value, entry)
    client = GiteaClient(auth)
    current, blob_sha = await _gitea_file(client, owner, repo, path)
    block = _relocate_block(value, path, original_start, expected_sha, current)
    updated = _replace_block(current, block, new_content)
    new_entry_id = await asyncio.to_thread(
        _projected_entry_id,
        value,
        path,
        updated,
        block[0] + 1,
    )
    new_sha = hashlib.sha256(new_content.rstrip("\r\n").encode("utf-8")).hexdigest()
    await client.request(
        "PUT",
        repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
        json={
            "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
            "sha": blob_sha,
            "message": f"Update entry {entry_id}",
        },
    )
    return success(
        {
            "message": f"Updated entry {entry_id}",
            "entry_hash": new_entry_id,
            "new_entry_hash": new_entry_id,
            "new_sha256sum": new_sha,
        }
    )


@router.delete("/source-slice")
async def delete_source_slice(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    entry_id = str(body.get("entry_hash") or "")
    expected_sha = str(body.get("sha256sum") or "")
    entry = _resolve_entry(value, entry_id)
    path, original_start = _source_path(value, entry)
    client = GiteaClient(auth)
    current, blob_sha = await _gitea_file(client, owner, repo, path)
    block = _relocate_block(value, path, original_start, expected_sha, current)
    updated = _replace_block(current, block, None)
    await client.request(
        "PUT",
        repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
        json={
            "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
            "sha": blob_sha,
            "message": f"Delete entry {entry_id}",
        },
    )
    return success(
        {"message": f"Deleted entry {entry_id}", "entry_hash": entry_id}
    )


@router.delete("/source-slices")
async def delete_source_slices(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    requested = body.get("entries")
    if not isinstance(requested, list) or not requested:
        raise ServiceError(400, "entries must contain at least one entry")
    grouped: defaultdict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for index, item in enumerate(requested):
        if not isinstance(item, dict):
            raise ServiceError(400, f"entries[{index}] must be an object")
        entry_id = str(item.get("entry_hash") or "")
        entry = _resolve_entry(value, entry_id)
        path, start = _source_path(value, entry)
        grouped[path].append((entry_id, str(item.get("sha256sum") or ""), start))

    client = GiteaClient(auth)
    operations = []
    for path, targets in grouped.items():
        current, blob_sha = await _gitea_file(client, owner, repo, path)
        located = [
            _relocate_block(value, path, start, expected_sha, current)
            for _, expected_sha, start in targets
        ]
        updated = current
        for block in sorted(located, key=lambda item: item[0], reverse=True):
            updated = _replace_block(updated, block, None)
        operations.append(
            {
                "operation": "update",
                "path": path,
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
            }
        )
    deleted = [str(item["entry_hash"]) for item in requested]
    await client.request(
        "POST",
        repo_path(owner, repo, "/contents"),
        json={
            "files": operations,
            "message": f"Delete {len(deleted)} ledger entries",
        },
    )
    return success(
        {
            "message": f"Deleted {len(deleted)} entries: {', '.join(deleted)}",
            "deleted_hashes": deleted,
        }
    )

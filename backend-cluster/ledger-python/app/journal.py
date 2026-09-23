from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import datetime
import hashlib
import posixpath
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from beancount.core.amount import Amount
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
from beancount.core.inventory import Inventory
from bea_engine.managed_price_cache import managed_source_for_path
from fastapi import APIRouter, Depends, Query
from fava.beans.funcs import hash_entry
from fava.beans.str import to_string

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import GiteaClient, repo_path, safe_repo_file_path
from .ledger import (
    LoadedLedger,
    add_includes,
    commit_sha_of,
    load_snapshot,
    project_error_summary,
    refuse_projected_errors,
    serialize_error,
)
from .main_support import load_ledger
from .serializers import json_value, relative_filename, source_files
from .workspace import cloned_workspace, write_text_copy_on_write


router = APIRouter(prefix="/journal/{owner}/{repo}")
PLAINTEXT_EXCLUDED_FLAGS = {"P", "S", "T", "C", "U", "R", "M"}
BEAN_FILE_RE = re.compile(r"\.(?:bean|beancount)$", re.IGNORECASE)
PUSHTAG_RE = re.compile(r"^pushtag[^\S\r\n]+#(\S+)\s*$")
POPTAG_RE = re.compile(r"^poptag[^\S\r\n]+#(\S+)\s*$")
PUSHMETA_RE = re.compile(r"^pushmeta[^\S\r\n]+([a-z][A-Za-z0-9_-]*):(.*)$")
POPMETA_RE = re.compile(r"^popmeta[^\S\r\n]+([a-z][A-Za-z0-9_-]*):?\s*$")
META_LINE_RE = re.compile(r"^[ \t]+([a-z][A-Za-z0-9_-]*):")
TAG_IN_HEADER_RE = re.compile(r"#(\S+)")
TXN_OR_DOC_HEADER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(?:txn|document|[*!#?%PSTCURM])(?=\s|$)")


async def loaded(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> LoadedLedger:
    return await load_ledger(owner, repo, auth)


def entry_ids_for_entries(entries: list[Any]) -> dict[int, str]:
    counts: defaultdict[str, int] = defaultdict(int)
    result: dict[int, str] = {}
    for entry in entries:
        base = hash_entry(entry)
        occurrence = counts[base]
        counts[base] += 1
        result[id(entry)] = base if occurrence == 0 else f"{base}:{occurrence}"
    return result


_entry_ids = entry_ids_for_entries


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


def _managed_source(entry: Any) -> str | None:
    filename = (entry.meta or {}).get("filename")
    if not isinstance(filename, str):
        return None
    return managed_source_for_path(Path(filename))


def _source_slice(value: LoadedLedger, entry: Any) -> tuple[str, str]:
    meta = entry.meta or {}
    filename = meta.get("filename")
    lineno = meta.get("lineno")
    if not isinstance(filename, str) or not isinstance(lineno, int) or lineno < 1:
        raise ServiceError(404, "Entry source is unavailable")
    source = Path(filename).resolve()
    if _managed_source(entry) is None:
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


def _entry_source_public(value: LoadedLedger, entry: Any) -> dict[str, Any] | None:
    meta = entry.meta or {}
    filename = meta.get("filename")
    lineno = meta.get("lineno")
    if not isinstance(filename, str) or not isinstance(lineno, int) or lineno < 1:
        return None
    try:
        source_text, digest = _source_slice(value, entry)
    except ServiceError:
        return None
    return {
        "path": relative_filename(filename, value.snapshot.root),
        "lineno": lineno,
        "text": source_text,
        "sha256sum": digest,
    }


def _source_path(value: LoadedLedger, entry: Any) -> tuple[str, int]:
    meta = entry.meta or {}
    filename = meta.get("filename")
    lineno = meta.get("lineno")
    if not isinstance(filename, str) or not isinstance(lineno, int) or lineno < 1:
        raise ServiceError(404, "Entry source is unavailable")
    managed_source = _managed_source(entry)
    if managed_source is not None:
        raise ServiceError(
            400,
            f"Managed price source {managed_source} is read-only; "
            "declare a price directive in a repository file to override it",
        )
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


def _apply_push_scopes(file_content: str, start_line: int, slice_text: str) -> str:
    """Materialize open pushtag/pushmeta scopes onto a slice leaving its file."""
    active_tags: list[str] = []
    active_meta: dict[str, str] = {}
    for line in file_content.splitlines()[:start_line]:
        stripped = line.strip()
        if m := PUSHTAG_RE.match(stripped):
            tag = m.group(1)
            if tag not in active_tags:
                active_tags.append(tag)
        elif m := POPTAG_RE.match(stripped):
            tag = m.group(1)
            if tag in active_tags:
                active_tags.remove(tag)
        elif m := PUSHMETA_RE.match(stripped):
            active_meta[m.group(1)] = m.group(2).strip()
        elif m := POPMETA_RE.match(stripped):
            active_meta.pop(m.group(1), None)

    if not active_tags and not active_meta:
        return slice_text

    lines = slice_text.splitlines()
    if not lines:
        return slice_text

    if active_tags and TXN_OR_DOC_HEADER_RE.match(lines[0]):
        existing_tags = set(TAG_IN_HEADER_RE.findall(lines[0]))
        missing_tags = [tag for tag in active_tags if tag not in existing_tags]
        if missing_tags:
            lines[0] = f"{lines[0].rstrip()} {' '.join(f'#{t}' for t in missing_tags)}"

    if active_meta:
        existing_keys = {
            m.group(1)
            for line in lines[1:]
            if (m := META_LINE_RE.match(line)) is not None
        }
        meta_lines = [
            f"  {key}: {val}"
            for key, val in active_meta.items()
            if key not in existing_keys
        ]
        if meta_lines:
            lines = [lines[0], *meta_lines, *lines[1:]]

    return "\n".join(lines)


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


async def _gitea_file_optional(
    client: GiteaClient,
    owner: str,
    repo: str,
    path: str,
) -> tuple[str | None, str | None]:
    try:
        return await _gitea_file(client, owner, repo, path)
    except ServiceError as error:
        if error.status == 404:
            return None, None
        raise


def _project_writes(
    value: LoadedLedger,
    updates: dict[str, str | None],
    action: str,
    allow_invalid: bool,
    resolve_at: tuple[str, int] | None = None,
) -> tuple[dict[str, Any], str | None]:
    before_errors = [
        serialize_error(item, value.snapshot.root) for item in value.fava.errors
    ]
    with cloned_workspace(
        value.snapshot.root, "beancount-source-edit-"
    ) as root:
        for path, content in updates.items():
            target = root / path
            if content is None:
                target.unlink(missing_ok=True)
            else:
                write_text_copy_on_write(target, content)
        projected = load_snapshot(dataclasses.replace(value.snapshot, root=root))
        after_errors = [
            serialize_error(item, projected.snapshot.root)
            for item in projected.fava.errors
        ]
        summary = project_error_summary(before_errors, after_errors)
        if not allow_invalid:
            refuse_projected_errors(summary, action)

        resolved_id: str | None = None
        if resolve_at is not None:
            rel_target, target_lineno = resolve_at
            ids = _entry_ids(projected.entries)
            resolved_root = root.resolve()
            for entry in projected.entries:
                meta = entry.meta or {}
                filename = meta.get("filename")
                if not isinstance(filename, str) or meta.get("lineno") != target_lineno:
                    continue
                try:
                    relative = str(Path(filename).resolve().relative_to(resolved_root))
                except ValueError:
                    continue
                if relative == rel_target:
                    resolved_id = ids[id(entry)]
                    break
            if resolved_id is None:
                raise ServiceError(400, "Updated content does not contain a valid entry")
        return summary, resolved_id


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
        result.update(
            account=entry.account,
            amount=_amount(entry.amount),
            tolerance=str(entry.tolerance) if entry.tolerance is not None else None,
            diff_amount=_amount(entry.diff_amount),
        )
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


def _entry_count_label(count: int) -> str:
    return f"{count} {'entry' if count == 1 else 'entries'}"


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
    include_source: bool = False,
) -> dict[str, Any]:
    filtered = value.fava.get_filtered(account=account, filter=filter, time=time)
    entry_ids = _entry_ids(value.entries)
    paired = [
        (
            entry,
            serialize_directive(
                entry,
                value.snapshot.root,
                entry_ids.get(id(entry), hash_entry(entry)),
            ),
        )
        for entry in reversed(filtered.entries)
        if not (isinstance(entry, Transaction) and entry.flag in {"C", "S"})
    ]
    paired = [
        (entry, item)
        for entry, item in paired
        if _matches_display_filters(
            item,
            directive_types,
            transaction_subtypes,
            document_subtypes,
            custom_subtypes,
        )
    ]
    effective_limit = min(limit, 200) if include_source else limit
    page = paired[offset : offset + effective_limit]
    items = []
    for entry, item in page:
        if include_source:
            source = _entry_source_public(value, entry)
            if source is not None:
                item["source"] = source
        items.append(item)
    return success(
        {
            "items": items,
            "total": len(paired),
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
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
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
    eligible = [
        entry
        for entry in filtered.entries
        if isinstance(entry, Balance | Transaction)
        and not (isinstance(entry, Transaction) and entry.flag in PLAINTEXT_EXCLUDED_FLAGS)
    ]
    total = len(eligible)
    selected = eligible[offset:] if limit is None else eligible[offset : offset + limit]
    truncated = (offset + len(selected)) < total
    for entry in selected:
        content += f"{to_string(entry).rstrip(chr(10))}\n\n"
    return success({"content": content, "total": total, "truncated": truncated})


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
            "managed_source": _managed_source(entry),
            "generated_from": None,
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
    allow_invalid = body.get("allowInvalid") is True
    dry_run = body.get("dryRun") is True
    if not isinstance(new_content, str) or not new_content.strip():
        raise ServiceError(400, "new_content must contain an entry")
    entry = _resolve_entry(value, entry_id)
    path, original_start = _source_path(value, entry)
    client = GiteaClient(auth)
    current, blob_sha = await _gitea_file(client, owner, repo, path)
    block = _relocate_block(value, path, original_start, expected_sha, current)
    updated = _replace_block(current, block, new_content)
    unchanged = updated == current
    summary, new_entry_id = await asyncio.to_thread(
        _project_writes,
        value,
        {path: updated},
        "update",
        allow_invalid,
        (path, block[0] + 1),
    )
    assert new_entry_id is not None
    new_sha = hashlib.sha256(new_content.rstrip("\r\n").encode("utf-8")).hexdigest()
    commit_sha: str | None = None
    if not dry_run and not unchanged:
        resp = await client.request(
            "PUT",
            repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
            json={
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
                "message": f"Update entry {entry_id}",
            },
        )
        commit_sha = commit_sha_of(resp)
    result: dict[str, Any] = {
        "message": (
            f"Would update entry {entry_id}" if dry_run else f"Updated entry {entry_id}"
        ),
        "entry_hash": new_entry_id,
        "new_entry_hash": new_entry_id,
        "new_sha256sum": new_sha,
        "unchanged": unchanged,
        "files": [path],
        **summary,
    }
    if commit_sha:
        result["commit"] = commit_sha
    if dry_run:
        result["preview"] = [{"path": path, "text": new_content.rstrip("\r\n")}]
    return success(result)


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
    allow_invalid = body.get("allowInvalid") is True
    dry_run = body.get("dryRun") is True
    entry = _resolve_entry(value, entry_id)
    path, original_start = _source_path(value, entry)
    client = GiteaClient(auth)
    current, blob_sha = await _gitea_file(client, owner, repo, path)
    block = _relocate_block(value, path, original_start, expected_sha, current)
    updated = _replace_block(current, block, None)
    summary, _ = await asyncio.to_thread(
        _project_writes,
        value,
        {path: updated},
        "delete",
        allow_invalid,
    )
    commit_sha: str | None = None
    if not dry_run:
        resp = await client.request(
            "PUT",
            repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
            json={
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
                "message": f"Delete entry {entry_id}",
            },
        )
        commit_sha = commit_sha_of(resp)
    result: dict[str, Any] = {
        "message": (
            f"Would delete entry {entry_id}" if dry_run else f"Deleted entry {entry_id}"
        ),
        "entry_hash": entry_id,
        "files": [path],
        **summary,
    }
    if commit_sha:
        result["commit"] = commit_sha
    if dry_run:
        result["preview"] = [{"path": path, "text": block[2]}]
    return success(result)


@router.delete("/source-slices")
async def delete_source_slices(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    requested = body.get("entries")
    allow_invalid = body.get("allowInvalid") is True
    dry_run = body.get("dryRun") is True
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
    projected_updates: dict[str, str | None] = {}
    preview: list[dict[str, str]] = []
    for path, targets in grouped.items():
        current, blob_sha = await _gitea_file(client, owner, repo, path)
        located = [
            _relocate_block(value, path, start, expected_sha, current)
            for _, expected_sha, start in targets
        ]
        for block in located:
            preview.append({"path": path, "text": block[2]})
        updated = current
        for block in sorted(located, key=lambda item: item[0], reverse=True):
            updated = _replace_block(updated, block, None)
        projected_updates[path] = updated
        operations.append(
            {
                "operation": "update",
                "path": path,
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
                "sha": blob_sha,
            }
        )
    summary, _ = await asyncio.to_thread(
        _project_writes,
        value,
        projected_updates,
        "delete",
        allow_invalid,
    )
    deleted = [str(item["entry_hash"]) for item in requested]
    commit_sha: str | None = None
    if not dry_run:
        resp = await client.request(
            "POST",
            repo_path(owner, repo, "/contents"),
            json={
                "files": operations,
                "message": f"Delete {_entry_count_label(len(deleted))}",
            },
        )
        commit_sha = commit_sha_of(resp)
    result: dict[str, Any] = {
        "message": (
            f"Would delete {_entry_count_label(len(deleted))}: {', '.join(deleted)}"
            if dry_run
            else f"Deleted {_entry_count_label(len(deleted))}: {', '.join(deleted)}"
        ),
        "deleted_hashes": deleted,
        "files": list(grouped.keys()),
        **summary,
    }
    if commit_sha:
        result["commit"] = commit_sha
    if dry_run:
        result["preview"] = preview
    return success(result)


@router.post("/move-source-slices")
async def move_source_slices(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
    value: Annotated[LoadedLedger, Depends(loaded)],
) -> dict[str, Any]:
    requested = body.get("entries")
    to_path = body.get("to_path")
    include_from = body.get("include_from")
    allow_invalid = body.get("allowInvalid") is True
    dry_run = body.get("dryRun") is True

    if not isinstance(requested, list) or not requested:
        raise ServiceError(400, "entries must name at least one entry")
    if not isinstance(to_path, str) or not to_path:
        raise ServiceError(400, "to_path is required")
    safe_repo_file_path(to_path, "to_path")
    if not BEAN_FILE_RE.search(to_path):
        raise ServiceError(
            400,
            f"toPath {to_path} is not a ledger file; name a .bean or .beancount file",
        )

    loaded_sources = source_files(value)
    reached = to_path in loaded_sources
    if not reached and not (value.snapshot.root / to_path).exists():
        def _is_reached_by_glob() -> bool:
            with cloned_workspace(value.snapshot.root, "beancount-reach-") as root:
                write_text_copy_on_write(root / to_path, "\n")
                projected = load_snapshot(dataclasses.replace(value.snapshot, root=root))
                return to_path in source_files(projected)

        reached = await asyncio.to_thread(_is_reached_by_glob)
    if include_from is not None:
        if not isinstance(include_from, str) or not include_from:
            raise ServiceError(400, "include_from must be a file path")
        safe_repo_file_path(include_from, "include_from")
        if include_from not in loaded_sources:
            raise ServiceError(
                400,
                f"includeFrom {include_from} is not a file the ledger loads; "
                f"name one, such as {value.snapshot.entrypoint}",
            )
    elif not reached:
        raise ServiceError(
            400,
            f"{to_path} is not included by the ledger "
            f"({value.snapshot.entrypoint} does not reach it), so moved entries would count nowhere; "
            f"pass includeFrom (for example {value.snapshot.entrypoint}) to add its include in the same commit",
        )

    grouped: defaultdict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for index, item in enumerate(requested):
        if not isinstance(item, dict):
            raise ServiceError(400, f"entries[{index}] must be an object")
        entry_id = str(item.get("entry_hash") or "")
        entry = _resolve_entry(value, entry_id)
        path, start = _source_path(value, entry)
        if path == to_path:
            raise ServiceError(
                400,
                f"an entry already lives in {to_path}; move only entries from other files",
            )
        grouped[path].append((entry_id, str(item.get("sha256sum") or ""), start))

    client = GiteaClient(auth)
    file_order = {path: idx for idx, path in enumerate(loaded_sources)}
    moved_items: list[tuple[int, int, str, str, str]] = []
    projected_updates: dict[str, str | None] = {}
    blob_shas: dict[str, str | None] = {}

    for path, targets in grouped.items():
        current, blob_sha = await _gitea_file(client, owner, repo, path)
        blob_shas[path] = blob_sha
        located: list[tuple[str, tuple[int, int, str, str]]] = []
        for entry_id, expected_sha, start in targets:
            block = _relocate_block(value, path, start, expected_sha, current)
            located.append((entry_id, block))
            scoped_text = _apply_push_scopes(current, block[0], block[2])
            moved_items.append(
                (
                    file_order.get(path, len(loaded_sources)),
                    block[0],
                    entry_id,
                    path,
                    scoped_text,
                )
            )
        updated = current
        for _, block in sorted(located, key=lambda item: item[1][0], reverse=True):
            updated = _replace_block(updated, block, None)
        projected_updates[path] = updated

    moved_items.sort(key=lambda item: (item[0], item[1]))
    moved_hashes = [item[2] for item in moved_items]
    preview = [{"path": item[3], "text": item[4]} for item in moved_items]
    combined_block = "\n\n".join(item[4].rstrip("\r\n") for item in moved_items)

    dest_current, dest_sha = await _gitea_file_optional(client, owner, repo, to_path)
    blob_shas[to_path] = dest_sha
    if dest_current is None or not dest_current.strip():
        dest_updated = f"{combined_block}\n"
    else:
        eol = "\r\n" if "\r\n" in dest_current else "\n"
        body_block = combined_block.replace("\r\n", "\n").replace("\n", eol)
        dest_updated = f"{dest_current.rstrip(chr(13) + chr(10))}{eol}{eol}{body_block}{eol}"
    projected_updates[to_path] = dest_updated

    include_added = include_from is not None and not reached
    if include_added and include_from is not None:
        base_dir = posixpath.dirname(include_from) or "."
        rel_target = posixpath.relpath(to_path, base_dir)
        if include_from in projected_updates:
            base_content = projected_updates[include_from] or ""
        else:
            base_content, inc_sha = await _gitea_file(client, owner, repo, include_from)
            blob_shas[include_from] = inc_sha
        projected_updates[include_from] = add_includes(base_content, [rel_target])

    summary, _ = await asyncio.to_thread(
        _project_writes,
        value,
        projected_updates,
        "move",
        allow_invalid,
    )

    commit_sha: str | None = None
    if not dry_run:
        operations = []
        for path, updated_text in projected_updates.items():
            assert updated_text is not None
            encoded = base64.b64encode(updated_text.encode("utf-8")).decode("ascii")
            existing_sha = blob_shas.get(path)
            if existing_sha is None:
                operations.append(
                    {"operation": "create", "path": path, "content": encoded}
                )
            else:
                operations.append(
                    {
                        "operation": "update",
                        "path": path,
                        "content": encoded,
                        "sha": existing_sha,
                    }
                )
        resp = await client.request(
            "POST",
            repo_path(owner, repo, "/contents"),
            json={
                "files": operations,
                "message": f"Move {_entry_count_label(len(moved_hashes))} to {to_path}",
            },
        )
        commit_sha = commit_sha_of(resp)

    verb = "Would move" if dry_run else "Moved"
    suffix = ""
    if include_added and include_from is not None:
        inc_verb = "would include" if dry_run else "now includes"
        suffix = f"; {include_from} {inc_verb} it"
    result: dict[str, Any] = {
        "message": f"{verb} {_entry_count_label(len(moved_hashes))} to {to_path}{suffix}",
        "moved_hashes": moved_hashes,
        "include_added": include_added,
        "files": list(projected_updates.keys()),
        "preview": preview,
        **summary,
    }
    if commit_sha:
        result["commit"] = commit_sha
    return success(result)

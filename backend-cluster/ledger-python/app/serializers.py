from __future__ import annotations

import dataclasses
import datetime
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from fava.beans.funcs import hash_entry
from fava.core.group_entries import group_entries_by_type

from .ledger import LoadedLedger


PUBLIC_FAVA_OPTIONS = (
    "account_journal_include_children",
    "auto_reload",
    "collapse_pattern",
    "conversion_currencies",
    "currency_column",
    "default_page",
    "fiscal_year_end",
    "indent",
    "invert_income_liabilities_equity",
    "language",
    "locale",
    "show_accounts_with_zero_balance",
    "show_accounts_with_zero_transactions",
    "show_closed_accounts",
    "sidebar_show_queries",
    "unrealized",
    "upcoming_events",
    "uptodate_indicator_grey_lookback_days",
    "use_external_editor",
)


def relative_filename(value: str, root: Path) -> str:
    try:
        return str(Path(value).resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return value


def json_value(value: Any, root: Path) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, re.Pattern):
        return value.pattern
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: json_value(getattr(value, field.name), root)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): json_value(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item, root) for item in value]
    return str(value)


def beancount_options(loaded: LoadedLedger) -> dict[str, Any]:
    options = loaded.options
    return {
        "title": str(options.get("title") or ""),
        "name_assets": str(options.get("name_assets") or "Assets"),
        "name_liabilities": str(options.get("name_liabilities") or "Liabilities"),
        "name_equity": str(options.get("name_equity") or "Equity"),
        "name_income": str(options.get("name_income") or "Income"),
        "name_expenses": str(options.get("name_expenses") or "Expenses"),
        "account_current_conversions": str(
            options.get("account_current_conversions") or "Conversions:Current"
        ),
        "account_current_earnings": str(
            options.get("account_current_earnings") or "Earnings:Current"
        ),
        "render_commas": bool(options.get("render_commas", True)),
        "operating_currency": list(options.get("operating_currency") or []),
    }


def fava_options(loaded: LoadedLedger) -> dict[str, Any]:
    value = loaded.fava.fava_options
    return {
        name: json_value(getattr(value, name), loaded.snapshot.root)
        for name in PUBLIC_FAVA_OPTIONS
    }


def _has_explicit_default_file(loaded: LoadedLedger) -> bool:
    for entry in loaded.fava.all_entries_by_type.Custom:
        if getattr(entry, "type", None) != "beancountio-option":
            continue
        values = getattr(entry, "values", ())
        if values and str(values[0].value).replace("-", "_") == "default_file":
            return True
    return False


def bcio_options(loaded: LoadedLedger) -> dict[str, Any]:
    value = dataclasses.asdict(loaded.fava.bcio_options)
    if not _has_explicit_default_file(loaded):
        value["default_file"] = loaded.snapshot.entrypoint
    return value


def attributes(loaded: LoadedLedger) -> dict[str, list[str]]:
    value = loaded.fava.attributes
    return {
        "accounts": list(value.accounts),
        "tags": list(value.tags),
        "links": list(value.links),
        "years": list(value.years),
        "currencies": list(value.currencies),
        "payees": list(value.payees),
    }


def accounts(loaded: LoadedLedger) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, value in loaded.fava.accounts.items():
        meta = json_value(dict(value.meta), loaded.snapshot.root)
        filename = meta.get("filename") if isinstance(meta, dict) else None
        if isinstance(filename, str):
            meta["filename"] = relative_filename(filename, loaded.snapshot.root)
        last_entry = value.last_entry
        result[name] = {
            "close_date": value.close_date.isoformat() if value.close_date else None,
            "meta": meta,
            "uptodate_status": value.uptodate_status,
            "balance_string": value.balance_string,
            "last_entry": (
                {
                    "date": last_entry.date.isoformat(),
                    "entry_hash": last_entry.entry_hash,
                }
                if last_entry
                else None
            ),
        }
    return result


def source_files(loaded: LoadedLedger) -> list[str]:
    paths = {loaded.snapshot.entrypoint}
    for value in loaded.options.get("include", ()):
        if isinstance(value, str):
            paths.add(relative_filename(value, loaded.snapshot.root))
    return sorted(paths)


def entries_count(entries: list[Any]) -> list[dict[str, Any]]:
    grouped = group_entries_by_type(entries)
    return [
        {"type": name, "number": len(items)}
        for name, items in grouped._asdict().items()
    ]


def repository(value: dict[str, Any]) -> dict[str, Any]:
    permissions = value.get("permissions")
    return {
        "id": value.get("id"),
        "name": value.get("name"),
        "description": value.get("description"),
        "full_name": value.get("full_name"),
        "empty": value.get("empty"),
        "private": value.get("private"),
        "size": value.get("size"),
        "created_at": value.get("created_at") or "",
        "updated_at": value.get("updated_at") or "",
        "permissions": (
            {
                "admin": permissions.get("admin"),
                "pull": permissions.get("pull"),
                "push": permissions.get("push"),
            }
            if isinstance(permissions, dict)
            else None
        ),
    }


def file_content(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": value.get("name"),
        "path": value.get("path"),
        "type": value.get("type"),
        "sha": value.get("sha"),
        "size": value.get("size"),
        "content": value.get("content"),
        "encoding": value.get("encoding"),
        "last_commit_sha": value.get("last_commit_sha"),
        "last_committer_date": value.get("last_committer_date"),
        "last_author_date": value.get("last_author_date"),
    }


USER_FIELDS = (
    "id",
    "login",
    "full_name",
    "login_name",
    "email",
    "active",
    "is_admin",
    "created",
    "last_login",
    "source_id",
    "visibility",
    "restricted",
    "prohibit_login",
    "description",
)


def user(value: dict[str, Any], fields: tuple[str, ...] = USER_FIELDS) -> dict[str, Any]:
    return {
        name: value.get(name) if name in fields else None
        for name in USER_FIELDS
    }


def webhook(value: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value.get(name)
        for name in (
            "id",
            "type",
            "active",
            "authorization_header",
            "branch_filter",
            "config",
            "events",
            "created_at",
            "updated_at",
        )
    }


def public_key(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "fingerprint": value.get("fingerprint"),
        "key": value.get("key"),
        "last_used_at": value.get("last_used_at"),
        "title": value.get("title"),
        "created_at": value.get("created_at"),
    }


def token(value: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value.get(name)
        for name in (
            "id",
            "name",
            "scopes",
            "sha1",
            "token_last_eight",
            "created_at",
            "last_used_at",
        )
    }


def commit(value: dict[str, Any]) -> dict[str, Any]:
    repo_commit = value.get("commit")
    return {
        "sha": value.get("sha"),
        "author": user(value["author"]) if isinstance(value.get("author"), dict) else None,
        "committer": (
            user(value["committer"])
            if isinstance(value.get("committer"), dict)
            else None
        ),
        "commit": (
            {"message": repo_commit.get("message")}
            if isinstance(repo_commit, dict)
            else None
        ),
        "created": value.get("created"),
    }


def git_reference(value: dict[str, Any]) -> dict[str, Any]:
    git_object = value.get("object")
    return {
        "ref": value.get("ref"),
        "object": (
            {"sha": git_object.get("sha"), "type": git_object.get("type")}
            if isinstance(git_object, dict)
            else None
        ),
    }

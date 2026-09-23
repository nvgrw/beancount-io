from __future__ import annotations

import importlib
import os
import re
import sys
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from bea_engine.managed_load import ManagedSource, load_with_sources
from bea_engine.managed_price_cache import zero_next_refresh
from fava.core.loader import load_file
from fava.ledger import FavaLedger

from .errors import ServiceError
from .repository import RepositorySnapshot


@dataclass(frozen=True)
class LoadedLedger:
    entries: list[Any]
    errors: list[Any]
    options: dict[str, Any]
    fava: FavaLedger
    snapshot: RepositorySnapshot


_python_runtime_lock = threading.RLock()
_activated_module_roots: set[Path] = set()

_ENTRY_TAG_RE = re.compile(r' \(\d{4}-\d{2}-\d{2}, ".*"\)$', re.DOTALL)
_BALANCE_GOT_RE = re.compile(
    r"^(Balance (?:failed|exceeds explicit tolerance) for [^:]+:.*?expected [^,]*), got .*$",
    re.DOTALL,
)
_IMBALANCE_PREFIX = "Transaction does not balance"
_RESIDUAL_SUFFIX_RE = re.compile(r"residual (.+)$")
_INCLUDE_LINE_RE = re.compile(r"^\s*include\s")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _purge_repository_modules(cache_root: Path) -> None:
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        paths = getattr(module, "__path__", ())
        if (filename and _under(Path(filename), cache_root)) or any(
            _under(Path(path), cache_root) for path in paths
        ):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


@contextmanager
def _activated(snapshot: RepositorySnapshot) -> Iterator[None]:
    old_cwd = Path.cwd()
    additions = [snapshot.root]
    if snapshot.dependency_path is not None:
        additions.insert(0, snapshot.dependency_path)
    for root in _activated_module_roots:
        _purge_repository_modules(root)
    _activated_module_roots.clear()
    _activated_module_roots.update(path.resolve() for path in additions)
    for path in reversed(additions):
        sys.path.insert(0, str(path))
    os.chdir(snapshot.root)
    try:
        yield
    finally:
        os.chdir(old_cwd)
        for path in additions:
            try:
                sys.path.remove(str(path))
            except ValueError:
                pass


def load_snapshot(snapshot: RepositorySnapshot) -> LoadedLedger:
    with _python_runtime_lock, _activated(snapshot):
        entries, errors, options = load_file(snapshot.root / snapshot.entrypoint)
        fava = FavaLedger(entries, list(errors), options)
    return LoadedLedger(
        list(entries),
        list(errors),
        dict(options),
        fava,
        snapshot,
    )


def _public_managed_source(source: ManagedSource) -> dict[str, Any]:
    return {
        "url": source.url,
        "alias": source.alias,
        "includedFrom": [
            {
                "file": include.file,
                "line": include.line,
                "target": include.target,
            }
            for include in source.included_from
        ],
        "commodity": source.commodity,
        "quote": source.quote,
        "source": source.source,
        "revision": source.revision,
        "etag": source.etag,
        "observedAt": source.observed_at,
        "latestPriceDate": source.latest_price_date,
        "fetchedAt": source.fetched_at,
        "nextRefreshAt": source.next_refresh_at,
        "freshness": source.freshness,
        "error": source.error,
        "shadowedCount": source.shadowed_count,
    }


def read_managed_price_sources(
    snapshot: RepositorySnapshot,
    *,
    refresh_urls: Iterable[str] = (),
) -> list[dict[str, Any]]:
    with _python_runtime_lock, _activated(snapshot):
        for url in dict.fromkeys(refresh_urls):
            zero_next_refresh(url)
        loaded = load_with_sources(snapshot.root / snapshot.entrypoint)
    return [_public_managed_source(source) for source in loaded.sources]


def serialize_error(value: Any, root: Path) -> dict[str, Any]:
    source = getattr(value, "source", None)
    filename: str | None = None
    lineno: int | None = None
    if isinstance(source, dict):
        raw_filename = source.get("filename")
        raw_lineno = source.get("lineno")
        if isinstance(raw_filename, str):
            try:
                filename = str(Path(raw_filename).resolve().relative_to(root.resolve()))
            except (OSError, ValueError):
                filename = raw_filename
        if isinstance(raw_lineno, int):
            lineno = raw_lineno
    public_source = (
        {"filename": filename, "lineno": lineno}
        if filename is not None and lineno is not None
        else None
    )
    raw_severity = getattr(value, "severity", None)
    severity = "warning" if raw_severity == "warning" else "error"
    return {
        "source": public_source,
        "message": str(getattr(value, "message", value)),
        "code": getattr(value, "code", None),
        "hint": getattr(value, "hint", None),
        "severity": severity,
    }


def _without_entry_tag(message: str) -> str:
    text = _ENTRY_TAG_RE.sub("", message)
    return _BALANCE_GOT_RE.sub(r"\1", text)


def _error_key(error: dict[str, Any]) -> tuple[str, str]:
    source = error.get("source")
    filename = source.get("filename", "") if isinstance(source, dict) else ""
    return (_without_entry_tag(str(error.get("message") or "")), str(filename or ""))


def count_ledger_errors(errors: Sequence[dict[str, Any]]) -> int:
    return sum(1 for item in errors if item.get("severity") != "warning")


def new_ledger_errors(
    before: Sequence[dict[str, Any]],
    after: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    remaining = Counter(_error_key(item) for item in before)
    introduced: list[dict[str, Any]] = []
    for item in after:
        if item.get("severity") == "warning":
            continue
        key = _error_key(item)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            introduced.append(item)
    return introduced


def project_error_summary(
    before: Sequence[dict[str, Any]],
    after: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "errors_before": count_ledger_errors(before),
        "errors_after": count_ledger_errors(after),
        "new_errors": new_ledger_errors(before, after),
    }


def refuse_projected_errors(summary: dict[str, Any], action: str) -> None:
    introduced: list[dict[str, Any]] = summary["new_errors"]
    if not introduced:
        return
    if len(introduced) == 1:
        only = introduced[0]
        text = _without_entry_tag(str(only.get("message") or ""))
        match = _RESIDUAL_SUFFIX_RE.search(text)
        if text.startswith(_IMBALANCE_PREFIX) and match is not None:
            source = only.get("source")
            msg = str(only.get("message") or text)
            if isinstance(source, dict) and source.get("filename") and source.get("lineno"):
                msg = f"{msg} ({source['filename']}:{source['lineno']})"
            raise ServiceError(
                400,
                msg,
                "UNBALANCED_TRANSACTION",
                {"residual": match.group(1)},
            )
    first_msg = str(introduced[0].get("message") or "Ledger validation failed")
    raise ServiceError(
        400,
        f"Cannot {action}: write introduces {len(introduced)} ledger "
        f"{'error' if len(introduced) == 1 else 'errors'}: {first_msg}",
        "NEW_LEDGER_ERRORS",
        {"new_errors": introduced},
    )


def commit_sha_of(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    commit = value.get("commit")
    if isinstance(commit, dict) and isinstance(commit.get("sha"), str):
        return commit["sha"]
    return None


def add_includes(content: str, paths: Sequence[str]) -> str:
    if not paths:
        return content
    eol = "\r\n" if "\r\n" in content else "\n"
    block = [
        f'include "{path.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for path in paths
    ]
    raw_lines = content.split("\n")
    lines = [
        line[:-1] if eol == "\r\n" and line.endswith("\r") else line
        for line in raw_lines
    ]
    last = -1
    for idx, line in enumerate(lines):
        if _INCLUDE_LINE_RE.match(line):
            last = idx
    if last >= 0:
        lines[last + 1 : last + 1] = block
        return eol.join(lines)
    body = content.rstrip("\r\n")
    prefix = f"{body}{eol}" if body else ""
    return f"{prefix}{eol.join(block)}{eol}"


def plugin_names(loaded: LoadedLedger) -> list[str]:
    plugins = loaded.options.get("plugin", [])
    return [
        str(item[0])
        for item in plugins
        if isinstance(item, (list, tuple)) and len(item) >= 1
    ]

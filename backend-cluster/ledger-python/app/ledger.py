from __future__ import annotations

import importlib
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from fava.core.loader import load_file
from fava.ledger import FavaLedger

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
    return {
        "source": public_source,
        "message": str(getattr(value, "message", value)),
        "code": getattr(value, "code", None),
        "hint": getattr(value, "hint", None),
    }


def plugin_names(loaded: LoadedLedger) -> list[str]:
    plugins = loaded.options.get("plugin", [])
    return [
        str(item[0])
        for item in plugins
        if isinstance(item, (list, tuple)) and len(item) >= 1
    ]

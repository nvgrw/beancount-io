from __future__ import annotations

import json
import multiprocessing
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import repository
from app.errors import ServiceError
from app.ledger import load_snapshot, plugin_names
from app.repository import (
    RepositorySnapshot,
    _dependency_target,
    _evict_ready_directories,
    _locked,
    _locks,
    _validate_ledger_sources,
    resolve_entrypoint,
)


def snapshot(root: Path) -> RepositorySnapshot:
    return RepositorySnapshot(
        owner="alice",
        repo="book",
        sha="a" * 40,
        root=root,
        entrypoint=resolve_entrypoint(root),
    )


def _hold_cross_process_lock(
    active: object,
    overlapped: object,
) -> None:
    with _locked("cross-process"):
        active.value += 1
        if active.value > 1:
            overlapped.value = 1
        time.sleep(0.03)
        active.value -= 1


def test_entrypoint_defaults_to_main_bean(tmp_path: Path) -> None:
    (tmp_path / "main.bean").write_text("", encoding="utf-8")

    assert resolve_entrypoint(tmp_path) == "main.bean"


def test_entrypoint_can_be_configured(tmp_path: Path) -> None:
    (tmp_path / ".beancountio.json").write_text(
        json.dumps({"entrypoint": "books/root.beancount"}), encoding="utf-8"
    )

    assert resolve_entrypoint(tmp_path) == "books/root.beancount"


def test_repository_python_plugin_executes(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    (plugins / "proof.py").write_text(
        """
from collections import namedtuple

PluginError = namedtuple("PluginError", "source message entry")
__plugins__ = ("proof",)

def proof(entries, options_map, config=None):
    return entries, [PluginError({"filename": __file__, "lineno": 1}, "repository plugin executed", None)]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "main.bean").write_text(
        'plugin "plugins.proof"\n', encoding="utf-8"
    )

    loaded = load_snapshot(snapshot(tmp_path))

    assert plugin_names(loaded) == ["plugins.proof"]
    assert [error.message for error in loaded.errors] == ["repository plugin executed"]


def test_repository_dependencies_install_atomically(tmp_path: Path) -> None:
    (tmp_path / ".beancountio-requirements.txt").write_text(
        "example-package==1.0\n", encoding="utf-8"
    )

    def install(command: list[str], **_kwargs: object) -> None:
        target = Path(command[command.index("--target") + 1])
        (target / "installed.txt").write_text("ok", encoding="utf-8")

    test_settings = replace(repository.settings, cache_root=tmp_path / "cache")
    with patch("app.repository.settings", test_settings):
        with patch("app.repository.subprocess.run", side_effect=install):
            target = _dependency_target(tmp_path)

    assert target is not None
    assert (target / ".ready").is_file()
    assert (target / "installed.txt").read_text(encoding="utf-8") == "ok"


def test_source_limits_follow_globs_but_ignore_unreferenced_files(
    tmp_path: Path,
) -> None:
    books = tmp_path / "books"
    books.mkdir()
    (tmp_path / "main.bean").write_text(
        'include "books/*.bean"\n', encoding="utf-8"
    )
    (books / "one.bean").write_text(
        "2000-01-01 open Assets:Cash USD\n", encoding="utf-8"
    )
    (tmp_path / "receipt.pdf").write_bytes(b"x" * 100)

    with patch.object(repository, "MAX_LEDGER_SOURCE_BYTES", 80):
        _validate_ledger_sources(tmp_path, "main.bean")


def test_source_file_count_limit_includes_transitive_globs(tmp_path: Path) -> None:
    books = tmp_path / "books"
    books.mkdir()
    (tmp_path / "main.bean").write_text(
        'include "books/*.bean"\n', encoding="utf-8"
    )
    for name in ("one.bean", "two.bean"):
        (books / name).write_text("", encoding="utf-8")

    with patch.object(repository, "MAX_LEDGER_SOURCE_FILES", 2):
        try:
            _validate_ledger_sources(tmp_path, "main.bean")
        except ServiceError as error:
            assert error.status == 413
            assert error.code == "RESOURCE_LIMIT_REACHED"
            assert error.details == {"maximum": 2, "current": 3}
        else:
            raise AssertionError("transitive source count should exceed the limit")


def test_source_file_count_includes_unreferenced_bean_files(tmp_path: Path) -> None:
    (tmp_path / "main.bean").write_text("", encoding="utf-8")
    (tmp_path / "unreferenced.bean").write_text("", encoding="utf-8")

    with patch.object(repository, "MAX_LEDGER_SOURCE_FILES", 1):
        try:
            _validate_ledger_sources(tmp_path, "main.bean")
        except ServiceError as error:
            assert error.details == {"maximum": 1, "current": 2}
        else:
            raise AssertionError("unreferenced ledger file should count")


def test_source_file_size_limit_is_enforced_before_parse(tmp_path: Path) -> None:
    (tmp_path / "main.bean").write_text("x" * 11, encoding="utf-8")

    with patch.object(repository, "MAX_LEDGER_SOURCE_FILE_BYTES", 10):
        try:
            _validate_ledger_sources(tmp_path, "main.bean")
        except ServiceError as error:
            assert error.status == 413
            assert error.details == {"maximum": 10, "current": 11}
        else:
            raise AssertionError("oversized source should have been rejected")


def test_keyed_locks_are_removed_after_last_user() -> None:
    with _locked("test-key"):
        assert "test-key" in _locks

    assert "test-key" not in _locks


def test_keyed_locks_exclude_other_worker_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    active = context.RawValue("i", 0)
    overlapped = context.RawValue("i", 0)
    processes = [
        context.Process(target=_hold_cross_process_lock, args=(active, overlapped))
        for _ in range(4)
    ]
    test_settings = replace(repository.settings, cache_root=tmp_path / "cache")

    with patch("app.repository.settings", test_settings):
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=3)

    assert all(process.exitcode == 0 for process in processes)
    assert overlapped.value == 0
    assert len(list((test_settings.cache_root / "locks").glob("*.lock"))) <= 1


def test_disk_cache_evicts_oldest_ready_directory(tmp_path: Path) -> None:
    roots = [tmp_path / name for name in ("old", "middle", "current")]
    for index, root in enumerate(roots):
        root.mkdir()
        marker = root / ".ready"
        marker.touch()
        os.utime(marker, (index + 1, index + 1))
    test_settings = replace(repository.settings, cache_eviction_grace_seconds=0)

    with patch("app.repository.settings", test_settings):
        _evict_ready_directories(tmp_path, 2, {roots[-1].resolve()})

    assert not roots[0].exists()
    assert roots[1].is_dir()
    assert roots[2].is_dir()

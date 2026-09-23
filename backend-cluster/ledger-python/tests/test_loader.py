from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import repository
from app.ledger import load_snapshot, plugin_names
from app.repository import RepositorySnapshot, _dependency_target, resolve_entrypoint


def snapshot(root: Path) -> RepositorySnapshot:
    return RepositorySnapshot(
        owner="alice",
        repo="book",
        sha="a" * 40,
        root=root,
        entrypoint=resolve_entrypoint(root),
    )


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

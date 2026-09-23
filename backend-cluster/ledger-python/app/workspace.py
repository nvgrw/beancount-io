from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .config import settings


_LINK_FALLBACK_ERRORS = {
    errno.EACCES,
    errno.EPERM,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _hardlink_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError as error:
        if error.errno not in _LINK_FALLBACK_ERRORS:
            raise
    return shutil.copy2(source, destination)


def clone_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, copy_function=_hardlink_or_copy)


@contextmanager
def cloned_workspace(source: Path, prefix: str) -> Generator[Path, None, None]:
    parent = settings.cache_root / "workspaces"
    parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 60 * 60
    for child in parent.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except FileNotFoundError:
            pass
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
        root = Path(directory) / "repository"
        clone_tree(source, root)
        yield root


def write_text_copy_on_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.write_text(content, encoding="utf-8")

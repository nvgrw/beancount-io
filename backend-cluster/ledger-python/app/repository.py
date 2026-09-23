from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import settings
from .errors import ServiceError


SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONFIG_PATH = ".beancountio.json"
DEFAULT_ENTRYPOINT = "main.bean"
MAX_CONFIG_BYTES = 16 * 1024
MAX_LEDGER_SOURCE_FILE_BYTES = 8 * 1024 * 1024
MAX_LEDGER_SOURCE_BYTES = 32 * 1024 * 1024
MAX_LEDGER_SOURCE_FILES = 4_096
MAX_INCLUDE_PATTERN_LENGTH = 1_024
MAX_INCLUDE_WILDCARDS = 64
LOCK_STRIPES = 64
INCLUDE_RE = re.compile(r'^[^\S\r\n]*include[^\S\r\n]+"([^"]*)"', re.MULTILINE)
URL_INCLUDE_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@dataclass(frozen=True)
class RepositorySnapshot:
    owner: str
    repo: str
    sha: str
    root: Path
    entrypoint: str
    dependency_path: Path | None = None


_locks_guard = threading.Lock()
_locks: dict[str, _LockState] = {}


class _LockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


@contextmanager
def _locked(key: str) -> Generator[None, None, None]:
    with _locks_guard:
        state = _locks.setdefault(key, _LockState())
        state.users += 1
    state.lock.acquire()
    try:
        lock_root = settings.cache_root / "locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        stripe = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % LOCK_STRIPES
        with (lock_root / f"{stripe:02d}.lock").open("a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        state.lock.release()
        with _locks_guard:
            state.users -= 1
            if state.users == 0 and _locks.get(key) is state:
                _locks.pop(key, None)


def _resource_limit(name: str, maximum: int, current: int) -> ServiceError:
    return ServiceError(
        413,
        f"{name} limit reached. Maximum: {maximum}, Current: {current}.",
        "RESOURCE_LIMIT_REACHED",
        {"maximum": maximum, "current": current},
    )


def _resolve_include(including_path: str, target: str) -> str:
    joined = target if target.startswith("/") else posixpath.join(
        posixpath.dirname(including_path), target
    )
    resolved = posixpath.normpath(joined).lstrip("/")
    if resolved == ".." or resolved.startswith("../"):
        raise ServiceError(400, f"Ledger include escapes the repository: {target}")
    return resolved


def _validate_ledger_sources(root: Path, entrypoint: str) -> None:
    root = root.resolve()
    pending = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".bean", ".beancount"}
    ]
    if entrypoint not in pending:
        pending.append(entrypoint)
    seen: set[str] = set()
    total_bytes = 0
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        source = (root / relative).resolve()
        try:
            normalized = source.relative_to(root).as_posix()
        except ValueError as exc:
            raise ServiceError(
                400, f"Ledger source escapes the repository: {relative}"
            ) from exc
        if not source.is_file():
            continue
        seen.add(normalized)
        if len(seen) > MAX_LEDGER_SOURCE_FILES:
            raise _resource_limit(
                "Ledger source file count", MAX_LEDGER_SOURCE_FILES, len(seen)
            )
        size = source.stat().st_size
        if size > MAX_LEDGER_SOURCE_FILE_BYTES:
            raise _resource_limit(
                "Ledger source file bytes", MAX_LEDGER_SOURCE_FILE_BYTES, size
            )
        total_bytes += size
        if total_bytes > MAX_LEDGER_SOURCE_BYTES:
            raise _resource_limit(
                "Ledger source bytes", MAX_LEDGER_SOURCE_BYTES, total_bytes
            )
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ServiceError(400, f"Ledger source is not UTF-8: {normalized}") from exc
        for match in INCLUDE_RE.finditer(content):
            target = match.group(1)
            if URL_INCLUDE_RE.match(target):
                continue
            resolved = _resolve_include(normalized, target)
            wildcard_count = sum(resolved.count(char) for char in "*?[")
            if (
                len(resolved) > MAX_INCLUDE_PATTERN_LENGTH
                or wildcard_count > MAX_INCLUDE_WILDCARDS
            ):
                raise ServiceError(400, f"Ledger include pattern is too complex: {target}")
            if any(char in resolved for char in "*?["):
                for candidate in root.glob(resolved):
                    if candidate.is_file():
                        pending.append(candidate.resolve().relative_to(root).as_posix())
            elif (root / resolved).is_file():
                pending.append(resolved)


def _evict_ready_directories(
    parent: Path,
    max_entries: int,
    protected: set[Path],
) -> None:
    if not parent.is_dir():
        return
    ready: list[tuple[float, Path]] = []
    for child in parent.iterdir():
        marker = child / ".ready"
        if child.is_dir() and marker.is_file():
            ready.append((marker.stat().st_mtime, child.resolve()))
    excess = len(ready) - max_entries
    if excess <= 0:
        return
    cutoff = time.time() - settings.cache_eviction_grace_seconds
    for modified, child in sorted(ready):
        if excess <= 0:
            break
        if child in protected or modified > cutoff:
            continue
        shutil.rmtree(child, ignore_errors=True)
        excess -= 1


def _segment(value: str, name: str) -> str:
    if not SEGMENT_RE.fullmatch(value):
        raise ServiceError(400, f"Invalid {name}")
    return value.lower()


def _safe_entrypoint(value: object) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ServiceError(400, f"{CONFIG_PATH}.entrypoint must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./"):
        raise ServiceError(400, f"{CONFIG_PATH}.entrypoint must be repository-relative")
    if path.suffix not in {".bean", ".beancount"}:
        raise ServiceError(400, f"{CONFIG_PATH}.entrypoint must name a .bean or .beancount file")
    return value


def resolve_entrypoint(root: Path) -> str:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return DEFAULT_ENTRYPOINT
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ServiceError(413, "Ledger configuration is too large")
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(400, f"{CONFIG_PATH} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ServiceError(400, f"{CONFIG_PATH} must contain a JSON object")
    if "entrypoint" not in value:
        return DEFAULT_ENTRYPOINT
    return _safe_entrypoint(value["entrypoint"])


def _bare_repo(owner: str, repo: str) -> Path:
    safe_owner = _segment(owner, "repository owner")
    safe_repo = _segment(repo, "repository name")
    path = settings.repositories_root / safe_owner / f"{safe_repo}.git"
    if not path.is_dir():
        raise ServiceError(404, "Repository not found")
    return path


def _git(bare: Path, *args: str, text: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", f"--git-dir={bare}", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ServiceError(502, "Could not read the repository snapshot") from exc
    return result.stdout if text else ""


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
        raise ServiceError(400, "Repository archive contains an unsafe path")


def _extract_archive(bare: Path, sha: str, destination: Path) -> None:
    descriptor, archive_name = tempfile.mkstemp(
        prefix=f"archive-{sha[:12]}-", suffix=".tar", dir=settings.cache_root
    )
    os.close(descriptor)
    archive_path = Path(archive_name)
    try:
        with archive_path.open("wb") as output:
            subprocess.run(
                ["git", f"--git-dir={bare}", "archive", "--format=tar", sha],
                check=True,
                stdout=output,
                stderr=subprocess.PIPE,
            )
        with tarfile.open(archive_path, mode="r") as archive:
            for member in archive:
                _validate_member(member)
                archive.extract(member, destination, filter="data")
    except (subprocess.CalledProcessError, tarfile.TarError, OSError) as exc:
        raise ServiceError(502, "Could not extract the repository snapshot") from exc
    finally:
        archive_path.unlink(missing_ok=True)


def _dependency_target(root: Path) -> Path | None:
    requirements = root / ".beancountio-requirements.txt"
    if not requirements.is_file():
        return None
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    target = settings.cache_root / "packages" / digest
    marker = target / ".ready"
    with _locked(f"dependencies:{digest}"):
        if marker.is_file():
            marker.touch()
        else:
            settings.cache_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f"packages-{digest[:12]}-", dir=settings.cache_root))
            try:
                subprocess.run(
                    [
                        os.environ.get("PYTHON", "python"),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--target",
                        str(temporary),
                        "--requirement",
                        str(requirements),
                    ],
                    check=True,
                )
                (temporary / ".ready").touch()
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.rename(target)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ServiceError(500, "Could not install ledger Python dependencies") from exc
    _evict_ready_directories(
        target.parent,
        settings.dependency_cache_entries,
        {target.resolve()},
    )
    return target


def materialize(owner: str, repo: str, branch: str = "HEAD") -> RepositorySnapshot:
    bare = _bare_repo(owner, repo)
    sha = _git(bare, "rev-parse", "--verify", branch).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ServiceError(502, "Gitea returned an invalid commit ID")
    root = settings.cache_root / "repositories" / _segment(owner, "repository owner") / _segment(repo, "repository name") / sha
    marker = root / ".ready"
    validation_marker = root / ".validated-sources-v1"
    with _locked(f"snapshot:{owner}/{repo}:{sha}"):
        if not marker.is_file():
            root.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=root.parent))
            try:
                _extract_archive(bare, sha, temporary)
                temporary_entrypoint = resolve_entrypoint(temporary)
                _validate_ledger_sources(temporary, temporary_entrypoint)
                (temporary / ".ready").touch()
                (temporary / ".validated-sources-v1").touch()
                temporary.rename(root)
            except Exception:
                for child in sorted(temporary.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                temporary.rmdir()
                raise
        elif not validation_marker.is_file():
            existing_entrypoint = resolve_entrypoint(root)
            _validate_ledger_sources(root, existing_entrypoint)
            validation_marker.touch()
        marker.touch()
    entrypoint = resolve_entrypoint(root)
    entry = root / entrypoint
    if not entry.is_file():
        raise ServiceError(404, f"Ledger entry point not found: {entrypoint}")
    settings.cache_root.mkdir(parents=True, exist_ok=True)
    _evict_ready_directories(
        root.parent,
        settings.materialized_snapshots_per_repo,
        {root.resolve()},
    )
    return RepositorySnapshot(
        owner=owner,
        repo=repo,
        sha=sha,
        root=root,
        entrypoint=entrypoint,
        dependency_path=_dependency_target(root),
    )

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import settings
from .errors import ServiceError


SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONFIG_PATH = ".beancountio.json"
DEFAULT_ENTRYPOINT = "main.bean"
MAX_CONFIG_BYTES = 16 * 1024


@dataclass(frozen=True)
class RepositorySnapshot:
    owner: str
    repo: str
    sha: str
    root: Path
    entrypoint: str
    dependency_path: Path | None = None


_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


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
    with _lock_for(f"dependencies:{digest}"):
        if marker.is_file():
            return target
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
    return target


def materialize(owner: str, repo: str, branch: str = "HEAD") -> RepositorySnapshot:
    bare = _bare_repo(owner, repo)
    sha = _git(bare, "rev-parse", "--verify", branch).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ServiceError(502, "Gitea returned an invalid commit ID")
    root = settings.cache_root / "repositories" / _segment(owner, "repository owner") / _segment(repo, "repository name") / sha
    marker = root / ".ready"
    with _lock_for(f"snapshot:{owner}/{repo}:{sha}"):
        if not marker.is_file():
            root.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=root.parent))
            try:
                _extract_archive(bare, sha, temporary)
                marker.parent.mkdir(parents=True, exist_ok=True)
                (temporary / ".ready").touch()
                temporary.rename(root)
            except Exception:
                for child in sorted(temporary.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                temporary.rmdir()
                raise
    entrypoint = resolve_entrypoint(root)
    entry = root / entrypoint
    if not entry.is_file():
        raise ServiceError(404, f"Ledger entry point not found: {entrypoint}")
    settings.cache_root.mkdir(parents=True, exist_ok=True)
    return RepositorySnapshot(
        owner=owner,
        repo=repo,
        sha=sha,
        root=root,
        entrypoint=entrypoint,
        dependency_path=_dependency_target(root),
    )

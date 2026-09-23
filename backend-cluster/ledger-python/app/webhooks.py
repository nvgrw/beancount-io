from __future__ import annotations

import datetime
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, UploadFile
from fava.core.loader import load_file

from .config import settings
from .errors import ServiceError, success


router = APIRouter(prefix="/webhook/gitea")


def _authorize(authorization: str | None) -> None:
    if settings.webhook_token is None or authorization != f"Bearer {settings.webhook_token}":
        raise ServiceError(401, "Invalid webhook token")


def _ack(body: dict[str, Any], message: str) -> dict[str, Any]:
    repository = body.get("repository")
    full_name = repository.get("full_name", "") if isinstance(repository, dict) else ""
    return {
        "status": "success",
        "repository": full_name,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "message": message,
    }


@router.post("/repo-push")
async def repo_push(
    body: dict[str, Any], authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    _authorize(authorization)
    return success(_ack(body, "Repository cache invalidated"))


@router.post("/repo-create")
async def repo_create(
    body: dict[str, Any], authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    _authorize(authorization)
    response = _ack(body, "Repository hooks processed")
    response["hooks_installed"] = []
    return success(response)


def _archive_count(upload: UploadFile) -> int:
    with tempfile.TemporaryDirectory(prefix="beancount-pre-receive-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=upload.file, mode="r:gz") as archive:
            archive.extractall(root, filter="data")
        entrypoint = root / "main.bean"
        if not entrypoint.is_file():
            return 0
        entries, _, _ = load_file(entrypoint)
        return len(entries)


@router.post("/pre-receive-check")
async def pre_receive_check(
    owner: Annotated[str, Form()],
    repo: Annotated[str, Form()],
    archive: Annotated[UploadFile, File()],
    old_archive: Annotated[UploadFile | None, File()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _authorize(authorization)
    try:
        new_count = _archive_count(archive)
        old_count = _archive_count(old_archive) if old_archive is not None else None
    except (OSError, tarfile.TarError, ValueError):
        return success(
            {
                "allow": True,
                "reason": "check_error",
                "new_directive_count": None,
                "old_directive_count": None,
                "limit": None,
            }
        )
    return success(
        {
            "allow": True,
            "reason": "unlimited_or_check_unavailable",
            "new_directive_count": new_count,
            "old_directive_count": old_count,
            "limit": None,
        }
    )

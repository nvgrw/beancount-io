from __future__ import annotations

import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from .auth import RequestAuth, request_auth
from .config import settings
from .errors import ServiceError, success
from .gitea import GiteaClient, repo_path, safe_repo_file_path
from .serializers import file_content


router = APIRouter(prefix="/ledgers/{owner}/{repo}")
ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:zip|tar\.gz)$")


def _file_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("content"), dict):
        raise ServiceError(502, "Gitea returned an invalid file response")
    return file_content(value["content"])


@router.get("/files")
async def get_file(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    path: Annotated[str, Query()],
) -> dict[str, Any]:
    client = GiteaClient(auth)
    try:
        value = await client.request(
            "GET", repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}")
        )
    except ServiceError as error:
        if error.status == 404:
            return success(None)
        raise
    return success(None if isinstance(value, list) else file_content(value))


@router.post("/files-content")
async def get_files(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    files = body.get("files") or []
    for index, path in enumerate(files):
        safe_repo_file_path(path, f"files[{index}]")
    values = await GiteaClient(auth).request(
        "POST", repo_path(owner, repo, "/file-contents"), json={"files": files}
    )
    return success(
        [file_content(value) for value in values if isinstance(value, dict)]
    )


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def create_file(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    path = str(body.get("path") or "")
    value = await GiteaClient(auth).request(
        "POST",
        repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
        json={
            "content": body.get("content"),
            "message": body.get("message") or f"Create {path}",
        },
    )
    return success(_file_response(value))


@router.put("/files")
async def update_file(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    path = str(body.get("path") or "")
    value = await GiteaClient(auth).request(
        "PUT",
        repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
        json={
            "content": body.get("content"),
            "sha": body.get("sha"),
            "message": body.get("message") or f"Update {path}",
        },
    )
    return success(_file_response(value))


@router.delete("/files")
async def delete_file(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    path = str(body.get("path") or "")
    await GiteaClient(auth).request(
        "DELETE",
        repo_path(owner, repo, f"/contents/{safe_repo_file_path(path)}"),
        json={
            "sha": body.get("sha"),
            "message": body.get("message") or f"Delete {path}",
        },
    )
    return success(None)


@router.post("/change-files")
async def change_files(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    operations = body.get("files") or []
    for index, operation in enumerate(operations):
        safe_repo_file_path(operation.get("path"), f"files[{index}].path")
        if operation.get("from_path") is not None:
            safe_repo_file_path(
                operation["from_path"], f"files[{index}].from_path"
            )
    payload = {
        key: value
        for key, value in body.items()
        if key in {"files", "message", "branch", "new_branch"} and value is not None
    }
    await GiteaClient(auth).request(
        "POST", repo_path(owner, repo, "/contents"), json=payload
    )
    return success(None)


@router.get("/dirs")
async def list_directory(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    dir_path: str | None = None,
) -> dict[str, Any]:
    suffix = "/contents"
    if dir_path is not None:
        suffix += f"/{safe_repo_file_path(dir_path, 'dir_path')}"
    try:
        value = await GiteaClient(auth).request("GET", repo_path(owner, repo, suffix))
    except ServiceError as error:
        if error.status == 404:
            return success([])
        raise
    if not isinstance(value, list):
        raise ServiceError(404, f"{dir_path} is a file, expected a directory")
    return success([file_content(item) for item in value])


@router.get("/archive/{archive}")
async def archive(
    owner: str,
    repo: str,
    archive: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> Response:
    if not ARCHIVE_NAME.fullmatch(archive) or ".." in archive:
        raise ServiceError(400, "Invalid archive name")
    headers = {} if auth.anonymous or auth.header is None else {"Authorization": auth.header}
    url = (
        f"{settings.gitea_url}/api/v1"
        f"{repo_path(owner, repo, f'/archive/{safe_repo_file_path(archive, 'archive')}')}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.get(url, headers=headers)
    if upstream.status_code == 404:
        raise ServiceError(404, "Archive not found")
    if upstream.is_error:
        raise ServiceError(upstream.status_code, "Failed to download archive")
    response_headers = {}
    disposition = upstream.headers.get("content-disposition")
    if disposition:
        response_headers["content-disposition"] = disposition
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=response_headers,
    )

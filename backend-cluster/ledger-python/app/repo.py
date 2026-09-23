from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient, repo_path
from .serializers import commit, git_reference


router = APIRouter(prefix="/repo/{owner}/{repo}")


@router.get("/commits")
async def commits(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    sha: str | None = None,
    path: str | None = None,
    stat: bool | None = None,
    verification: bool | None = None,
    files: bool | None = None,
    page: int | None = None,
    limit: int | None = None,
    var_not: str | None = None,
    not_ref: Annotated[str | None, Query(alias="not")] = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET",
        repo_path(owner, repo, "/commits"),
        params={
            "sha": sha,
            "path": path,
            "stat": stat,
            "verification": verification,
            "files": files,
            "page": page,
            "limit": limit,
            "not": var_not or not_ref,
        },
    )
    return success([commit(value) for value in values])


@router.get("/git/refs")
async def git_refs(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET", repo_path(owner, repo, "/git/refs")
    )
    return success([git_reference(value) for value in values])

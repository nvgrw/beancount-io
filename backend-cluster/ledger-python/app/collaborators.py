from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient, path_segment, repo_path
from .serializers import user


router = APIRouter(prefix="/collaborators/{owner}/{repo}")


@router.get("")
async def list_collaborators(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET",
        repo_path(owner, repo, "/collaborators"),
        params={"page": page, "limit": limit},
    )
    return success([user(value) for value in values])


def _collaborator_path(owner: str, repo: str, collaborator: str) -> str:
    return repo_path(owner, repo, f"/collaborators/{path_segment(collaborator)}")


@router.get("/{collaborator}")
async def collaborator_permission(
    owner: str,
    repo: str,
    collaborator: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request(
        "GET", f"{_collaborator_path(owner, repo, collaborator)}/permission"
    )
    return success(
        {
            "permission": value.get("permission"),
            "role_name": value.get("role_name"),
            "user": user(value["user"]) if isinstance(value.get("user"), dict) else None,
        }
    )


@router.put("/{collaborator}")
async def update_collaborator(
    owner: str,
    repo: str,
    collaborator: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    payload = {}
    if body.get("permission") is not None:
        payload["permission"] = body["permission"]
    await GiteaClient(auth).request(
        "PUT", _collaborator_path(owner, repo, collaborator), json=payload
    )
    return success(None)


@router.delete("/{collaborator}")
async def delete_collaborator(
    owner: str,
    repo: str,
    collaborator: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request(
        "DELETE", _collaborator_path(owner, repo, collaborator)
    )
    return success(None)

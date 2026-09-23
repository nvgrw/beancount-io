from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient, path_segment
from .serializers import token


router = APIRouter(prefix="/tokens/{username}")


def _tokens_path(username: str, suffix: str = "") -> str:
    return f"/users/{path_segment(username)}/tokens{suffix}"


@router.get("")
async def list_tokens(
    username: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET", _tokens_path(username), params={"page": page, "limit": limit}
    )
    return success([token(value) for value in values])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_token(
    username: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    payload = {"name": body.get("name")}
    if body.get("scopes") is not None:
        payload["scopes"] = body["scopes"]
    value = await GiteaClient(auth).request(
        "POST", _tokens_path(username), json=payload
    )
    return success(token(value))


@router.delete("/{token}")
async def delete_token(
    username: str,
    token: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request(
        "DELETE", _tokens_path(username, f"/{path_segment(token)}")
    )
    return success(None)

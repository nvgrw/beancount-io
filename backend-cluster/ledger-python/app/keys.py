from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient
from .serializers import public_key


router = APIRouter(prefix="/keys")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request(
        "POST",
        "/user/keys",
        json={
            "key": body.get("key"),
            "title": body.get("title"),
            "read_only": body.get("read_only", False),
        },
    )
    return success(public_key(value))


@router.get("")
async def list_keys(
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET", "/user/keys", params={"page": page, "limit": limit}
    )
    return success([public_key(value) for value in values])


@router.get("/{key_id}")
async def get_key(
    key_id: int,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request("GET", f"/user/keys/{key_id}")
    return success(public_key(value))


@router.delete("/{key_id}")
async def delete_key(
    key_id: int,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request("DELETE", f"/user/keys/{key_id}")
    return success(None)

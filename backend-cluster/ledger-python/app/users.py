from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient
from .serializers import user


router = APIRouter(prefix="/user")


@router.get("/me")
async def current_user(
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    return success(user(await GiteaClient(auth).request("GET", "/user")))


@router.get("/search")
async def search_users(
    query: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    value = await GiteaClient(auth).request(
        "GET",
        "/users/search",
        params={"q": query, "page": page, "limit": limit},
    )
    values = value.get("data", []) if isinstance(value, dict) else value
    return success([user(item) for item in values])

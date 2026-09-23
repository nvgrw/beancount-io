from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from .auth import RequestAuth, request_auth
from .errors import success
from .gitea import GiteaClient, path_segment
from .serializers import repository, user, webhook


router = APIRouter(prefix="/admin")


@router.get("/ledgers/{id}")
async def get_ledger_by_repo_id(
    id: int,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request("GET", f"/repositories/{id}")
    return success(repository(value))


@router.post("/users")
async def create_user(
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request(
        "POST",
        "/admin/users",
        json={
            "username": body.get("username"),
            "password": body.get("password"),
            "email": body.get("email"),
            "must_change_password": False,
        },
    )
    fields = (
        "id",
        "is_admin",
        "email",
        "login",
        "source_id",
        "visibility",
        "restricted",
        "prohibit_login",
        "description",
    )
    return success(user(value, fields))


@router.delete("/users/{username}")
async def delete_user(
    username: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request(
        "DELETE",
        f"/admin/users/{path_segment(username)}",
        params={"purge": True},
    )
    return success(None)


@router.patch("/users/{username}")
async def edit_user(
    username: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    payload = {key: value for key, value in body.items() if value is not None}
    value = await GiteaClient(auth).request(
        "PATCH",
        f"/admin/users/{path_segment(username)}",
        json=payload,
    )
    fields = (
        "id",
        "is_admin",
        "email",
        "login",
        "full_name",
        "active",
        "created",
        "last_login",
    )
    return success(user(value, fields))


@router.post("/users/{username}/rename")
async def rename_user(
    username: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request(
        "POST",
        f"/admin/users/{path_segment(username)}/rename",
        json={"new_username": body.get("new_username")},
    )
    return success(None)


@router.post("/webhooks")
async def create_webhook(
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    payload = {key: value for key, value in body.items() if value is not None}
    payload.setdefault("active", False)
    value = await GiteaClient(auth).request("POST", "/admin/hooks", json=payload)
    return success(webhook(value))


@router.get("/webhooks")
async def list_webhooks(
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
    hook_type: Annotated[str | None, Query(alias="type")] = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET",
        "/admin/hooks",
        params={"page": page, "limit": limit, "type": hook_type},
    )
    return success([webhook(value) for value in values])


@router.get("/webhooks/{hook_id}")
async def get_webhook(
    hook_id: int,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    value = await GiteaClient(auth).request("GET", f"/admin/hooks/{hook_id}")
    return success(webhook(value))


@router.patch("/webhooks/{hook_id}")
async def update_webhook(
    hook_id: int,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    payload = {key: value for key, value in body.items() if value is not None}
    value = await GiteaClient(auth).request(
        "PATCH", f"/admin/hooks/{hook_id}", json=payload
    )
    return success(webhook(value))


@router.delete("/webhooks/{hook_id}")
async def delete_webhook(
    hook_id: int,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request("DELETE", f"/admin/hooks/{hook_id}")
    return success(None)

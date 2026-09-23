from __future__ import annotations

import base64
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from .auth import RequestAuth, request_auth
from .errors import ServiceError, success
from .gitea import GiteaClient, path_segment, repo_path
from .serializers import repository


router = APIRouter()


def slugify(value: str) -> str:
    value = value.lower().replace(" ", "-")
    value = re.sub(r"[^a-z0-9_-]", "", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def validate_name(value: str) -> None:
    if not value:
        raise ServiceError(400, "Ledger name must contain at least one alphanumeric character")
    if len(value) > 100:
        raise ServiceError(
            400,
            f"Ledger name is too long ({len(value)} characters). Maximum is 100 characters after transformation",
        )
    if not re.fullmatch(r"[a-z0-9_-]+", value):
        raise ServiceError(
            400,
            "Ledger name can only contain lowercase letters, numbers, underscores, and hyphens",
        )


async def _main_only_policy(client: GiteaClient, owner: str, repo: str) -> None:
    path = repo_path(owner, repo)
    branch_rules = await client.request("GET", f"{path}/branch_protections")
    existing = {
        rule.get("rule_name"): rule
        for rule in branch_rules
        if isinstance(rule, dict)
    }
    for rule in (
        {"rule_name": "main", "enable_push": True, "priority": 1},
        {"rule_name": "*", "enable_push": False, "priority": 2},
    ):
        current = existing.get(rule["rule_name"])
        if current is None:
            await client.request("POST", f"{path}/branch_protections", json=rule)
        elif (
            current.get("enable_push") != rule["enable_push"]
            or current.get("priority") != rule["priority"]
        ):
            await client.request(
                "PATCH",
                f"{path}/branch_protections/{path_segment(str(rule['rule_name']))}",
                json={
                    "enable_push": rule["enable_push"],
                    "priority": rule["priority"],
                },
            )
    tag_rules = await client.request("GET", f"{path}/tag_protections")
    tag_rule = next(
        (
            rule
            for rule in tag_rules
            if isinstance(rule, dict) and rule.get("name_pattern") == "*"
        ),
        None,
    )
    if tag_rule is None:
        tag_rule = await client.request(
            "POST",
            f"{path}/tag_protections",
            json={"name_pattern": "*", "whitelist_usernames": [owner]},
        )
    if isinstance(tag_rule, dict) and tag_rule.get("id") is not None:
        await client.request(
            "PATCH",
            f"{path}/tag_protections/{tag_rule['id']}",
            json={"whitelist_usernames": []},
        )


@router.get("/ledgers")
async def list_ledgers(
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET", "/user/repos", params={"page": page, "limit": limit}
    )
    return success([repository(value) for value in values])


@router.get("/ledgers/users/{username}")
async def list_user_ledgers(
    username: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    values = await GiteaClient(auth).request(
        "GET",
        f"/users/{path_segment(username)}/repos",
        params={"page": page, "limit": limit},
    )
    return success([repository(value) for value in values])


@router.get("/ledgers/search")
async def search_ledgers(
    auth: Annotated[RequestAuth, Depends(request_auth)],
    q: str | None = None,
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    value = await GiteaClient(auth).request(
        "GET", "/repos/search", params={"q": q, "page": page, "limit": limit}
    )
    return success(
        {
            "data": [repository(item) for item in value.get("data", [])],
            "ok": value.get("ok"),
        }
    )


@router.get("/ledgers/{owner}/{repo}")
async def get_ledger(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    return success(repository(await GiteaClient(auth).repository(owner, repo)))


@router.post("/ledgers", status_code=status.HTTP_201_CREATED)
async def create_ledger(
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise ServiceError(400, "Files are required")
    if not files.get("main.bean"):
        raise ServiceError(400, "main.bean is required")
    if not auth.username:
        raise ServiceError(401, "Unauthorized")
    name = slugify(str(body.get("name") or ""))
    validate_name(name)
    client = GiteaClient(auth)
    try:
        await client.repository(auth.username, name)
    except ServiceError as error:
        if error.status != 404:
            raise
    else:
        raise ServiceError(
            400,
            f"A ledger with the name '{name}' already exists. Please choose a different name.",
        )
    created = await client.request(
        "POST",
        "/user/repos",
        json={
            "name": name,
            "private": body.get("private"),
            "description": body.get("description"),
        },
    )
    try:
        await _main_only_policy(client, auth.username, name)
        await client.request(
            "POST",
            repo_path(auth.username, name, "/contents"),
            json={
                "files": [
                    {
                        "operation": "create",
                        "path": path,
                        "content": base64.b64encode(str(content).encode()).decode(),
                    }
                    for path, content in files.items()
                ]
            },
        )
    except Exception:
        await client.request("DELETE", repo_path(auth.username, name))
        raise
    return success(repository(created))


@router.put("/ledgers/{owner}/{repo}")
async def update_ledger(
    owner: str,
    repo: str,
    body: dict[str, Any],
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    update: dict[str, Any] = {}
    if body.get("name") is not None:
        update["name"] = slugify(str(body["name"]))
        validate_name(update["name"])
    for field in ("description", "private"):
        if body.get(field) is not None:
            update[field] = body[field]
    value = await GiteaClient(auth).request(
        "PATCH", repo_path(owner, repo), json=update
    )
    return success(repository(value))


@router.delete("/ledgers/{owner}/{repo}")
async def delete_ledger(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> dict[str, Any]:
    await GiteaClient(auth).request("DELETE", repo_path(owner, repo))
    return success(None)

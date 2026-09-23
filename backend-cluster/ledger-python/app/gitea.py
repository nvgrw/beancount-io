from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .auth import RequestAuth
from .config import settings
from .errors import ServiceError


class GiteaClient:
    def __init__(self, auth: RequestAuth) -> None:
        self._headers = {"Accept": "application/json"}
        if auth.header is not None:
            self._headers["Authorization"] = auth.header

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(
            base_url=f"{settings.gitea_url}/api/v1",
            headers=self._headers,
            timeout=30,
        ) as client:
            response = await client.request(method, path, json=json, params=params)
        if response.is_error:
            try:
                body = response.json()
                message = body.get("message") if isinstance(body, dict) else None
            except ValueError:
                message = None
            raise ServiceError(
                response.status_code,
                str(message or f"Gitea API error ({response.status_code})"),
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def repository(self, owner: str, repo: str) -> dict[str, Any]:
        value = await self.request("GET", repo_path(owner, repo))
        if not isinstance(value, dict):
            raise ServiceError(502, "Gitea returned an invalid repository response")
        return value


def path_segment(value: str) -> str:
    return quote(value, safe="")


def repo_path(owner: str, repo: str, suffix: str = "") -> str:
    return f"/repos/{path_segment(owner)}/{path_segment(repo)}{suffix}"


def safe_repo_file_path(value: object, field: str = "path") -> str:
    if not isinstance(value, str) or not value:
        raise ServiceError(400, "File path must not be empty")
    if "\x00" in value or "\\" in value:
        raise ServiceError(400, f"Invalid file path: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ServiceError(400, f"Invalid file path: {value}")
    return "/".join(path_segment(part) for part in parts)

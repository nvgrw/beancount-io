from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header

from .errors import ServiceError


@dataclass(frozen=True)
class RequestAuth:
    header: str | None
    anonymous: bool = False
    username: str | None = None


async def request_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> RequestAuth:
    if authorization == "Anonymous":
        return RequestAuth(header=None, anonymous=True)
    if authorization and (
        authorization.startswith("Basic ") or authorization.startswith("token ")
    ):
        username = None
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode()
                username = decoded.split(":", 1)[0]
            except (ValueError, UnicodeDecodeError):
                pass
        return RequestAuth(header=authorization, username=username)
    raise ServiceError(
        401,
        "No authorization header provided. Use 'Basic <credentials>' or 'token <token>'",
    )

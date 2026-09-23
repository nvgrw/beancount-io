from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ServiceError(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        details: dict[str, int | str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


def success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def error(message: str, code: str | None = None, details: dict[str, int | str] | None = None) -> dict[str, Any]:
    return {"success": False, "error": message, "code": code, "details": details}


async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=error(str(exc), exc.code, exc.details),
    )

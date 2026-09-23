from __future__ import annotations

import io
from typing import Annotated, Any

from beanquery.render.text import render as render_text
from fastapi import APIRouter, Depends
from fava.modules.query_shell import QueryShellModule

from .auth import RequestAuth, request_auth
from .errors import success
from .ledger import LoadedLedger
from .main_support import load_ledger
from .serializers import json_value


router = APIRouter(prefix="/shell/{owner}/{repo}")


async def loaded(
    owner: str,
    repo: str,
    auth: Annotated[RequestAuth, Depends(request_auth)],
) -> LoadedLedger:
    return await load_ledger(owner, repo, auth)


def _body_query(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return ""
    raw = body.get("query")
    return raw if isinstance(raw, str) else ""


def _execute_query_text(value: LoadedLedger, query: str) -> dict[str, Any]:
    module = QueryShellModule(value.fava)
    res = module.shell.run(value.entries, query)
    if isinstance(res, str):
        return {"text": res, "rowCount": None}
    buf = io.StringIO()
    rows = res.fetchall()
    render_text(
        res.description,
        rows,
        buf,
        dcontext=value.fava.options["dcontext"],
    )
    return {"text": buf.getvalue(), "rowCount": len(rows)}


@router.get("/query")
async def query_shell(
    value: Annotated[LoadedLedger, Depends(loaded)],
    query: str = "",
) -> dict[str, Any]:
    result = QueryShellModule(value.fava).execute_query_serialised(
        value.entries, query
    )
    return success({"result": json_value(result, value.snapshot.root)})


@router.post("/query")
async def query_shell_post(
    value: Annotated[LoadedLedger, Depends(loaded)],
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = QueryShellModule(value.fava).execute_query_serialised(
        value.entries, _body_query(body)
    )
    return success({"result": json_value(result, value.snapshot.root)})


@router.get("/query-text")
async def query_shell_text(
    value: Annotated[LoadedLedger, Depends(loaded)],
    query: str = "",
) -> dict[str, Any]:
    return success(_execute_query_text(value, query))


@router.post("/query-text")
async def query_shell_text_post(
    value: Annotated[LoadedLedger, Depends(loaded)],
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return success(_execute_query_text(value, _body_query(body)))

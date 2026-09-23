from __future__ import annotations

from typing import Annotated, Any

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


@router.get("/query")
async def query_shell(
    value: Annotated[LoadedLedger, Depends(loaded)],
    query: str,
) -> dict[str, Any]:
    result = QueryShellModule(value.fava).execute_query_serialised(
        value.entries, query
    )
    return success({"result": json_value(result, value.snapshot.root)})


@router.get("/query-text")
async def query_shell_text(
    value: Annotated[LoadedLedger, Depends(loaded)],
    query: str,
) -> dict[str, Any]:
    text = QueryShellModule(value.fava).execute_query_as_text(value.entries, query)
    return success({"text": text})

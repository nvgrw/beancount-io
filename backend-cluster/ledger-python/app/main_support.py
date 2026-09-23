from __future__ import annotations

import asyncio

from .auth import RequestAuth
from .gitea import GiteaClient
from .ledger import LoadedLedger, load_snapshot
from .repository import materialize


async def load_ledger(owner: str, repo: str, auth: RequestAuth) -> LoadedLedger:
    metadata = await GiteaClient(auth).repository(owner, repo)
    branch = metadata.get("default_branch") or "HEAD"
    snapshot = await asyncio.to_thread(materialize, owner, repo, str(branch))
    return await asyncio.to_thread(load_snapshot, snapshot)

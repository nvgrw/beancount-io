from __future__ import annotations

import asyncio

from .auth import RequestAuth
from .gitea import GiteaClient
from .ledger import LoadedLedger, load_snapshot
from .ledger_cache import ParsedLedgerCache
from .config import settings
from .repository import materialize


parsed_ledger_cache = ParsedLedgerCache(
    settings.parsed_ledger_cache_entries,
    settings.parsed_ledger_queue_depth,
)


async def load_ledger(owner: str, repo: str, auth: RequestAuth) -> LoadedLedger:
    metadata = await GiteaClient(auth).repository(owner, repo)
    branch = metadata.get("default_branch") or "HEAD"
    snapshot = await asyncio.to_thread(materialize, owner, repo, str(branch))
    return await parsed_ledger_cache.get(snapshot, load_snapshot)

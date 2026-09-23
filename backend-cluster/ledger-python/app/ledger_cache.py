from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fava.util.date import local_today

from .ledger import LoadedLedger
from .errors import ServiceError
from .repository import RepositorySnapshot


@dataclass(frozen=True)
class LedgerCacheKey:
    sha: str
    entrypoint: str
    effective_date: date
    root: Path


class ParsedLedgerCache:
    def __init__(self, max_entries: int, max_queue_depth: int = 32) -> None:
        self._max_entries = max(1, max_entries)
        self._max_queue_depth = max(1, max_queue_depth)
        self._values: OrderedDict[LedgerCacheKey, LoadedLedger] = OrderedDict()
        self._inflight: dict[LedgerCacheKey, asyncio.Task[LoadedLedger]] = {}
        self._lock = asyncio.Lock()
        self._parse_slot = asyncio.Semaphore(1)

    @staticmethod
    def key(
        snapshot: RepositorySnapshot,
        effective_date: date | None = None,
    ) -> LedgerCacheKey:
        return LedgerCacheKey(
            sha=snapshot.sha,
            entrypoint=snapshot.entrypoint,
            effective_date=effective_date or local_today(),
            root=snapshot.root.resolve(),
        )

    async def get(
        self,
        snapshot: RepositorySnapshot,
        loader: Callable[[RepositorySnapshot], LoadedLedger],
        *,
        effective_date: date | None = None,
    ) -> LoadedLedger:
        key = self.key(snapshot, effective_date)
        async with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                self._values.move_to_end(key)
                return cached
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= self._max_queue_depth:
                    raise ServiceError(503, "Parsed ledger queue is full")
                task = asyncio.create_task(self._load(key, snapshot, loader))
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _load(
        self,
        key: LedgerCacheKey,
        snapshot: RepositorySnapshot,
        loader: Callable[[RepositorySnapshot], LoadedLedger],
    ) -> LoadedLedger:
        try:
            async with self._parse_slot:
                loaded = await asyncio.to_thread(loader, snapshot)
        except BaseException:
            async with self._lock:
                self._inflight.pop(key, None)
            raise
        async with self._lock:
            self._inflight.pop(key, None)
            self._values[key] = loaded
            self._values.move_to_end(key)
            while len(self._values) > self._max_entries:
                self._values.popitem(last=False)
        return loaded

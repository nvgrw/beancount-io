import asyncio
import threading
import time
from datetime import date
from pathlib import Path

from app.ledger_cache import ParsedLedgerCache
from app.errors import ServiceError
from app.repository import RepositorySnapshot


def snapshot(root: Path, sha: str) -> RepositorySnapshot:
    return RepositorySnapshot(
        owner="alice",
        repo="book",
        sha=sha,
        root=root,
        entrypoint="main.bean",
    )


def test_reuses_parsed_ledger_for_same_snapshot(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(8)
    value = object()
    calls = 0

    def load(_snapshot: RepositorySnapshot):
        nonlocal calls
        calls += 1
        return value

    async def run() -> None:
        item = snapshot(tmp_path, "a" * 40)
        assert await cache.get(item, load) is value
        assert await cache.get(item, load) is value

    asyncio.run(run())
    assert calls == 1


def test_coalesces_concurrent_parses(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(8)
    value = object()
    calls = 0

    def load(_snapshot: RepositorySnapshot):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return value

    async def run() -> None:
        item = snapshot(tmp_path, "b" * 40)
        loaded = await asyncio.gather(*(cache.get(item, load) for _ in range(4)))
        assert all(result is value for result in loaded)

    asyncio.run(run())
    assert calls == 1


def test_effective_date_is_part_of_cache_key(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(8)
    calls = 0

    def load(_snapshot: RepositorySnapshot):
        nonlocal calls
        calls += 1
        return object()

    async def run() -> None:
        item = snapshot(tmp_path, "c" * 40)
        first = await cache.get(item, load, effective_date=date(2026, 9, 23))
        second = await cache.get(item, load, effective_date=date(2026, 9, 24))
        assert first is not second

    asyncio.run(run())
    assert calls == 2


def test_invalidate_removes_every_effective_date_variant(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(8)
    calls = 0

    def load(_snapshot: RepositorySnapshot):
        nonlocal calls
        calls += 1
        return object()

    async def run() -> None:
        item = snapshot(tmp_path, "d" * 40)
        await cache.get(item, load, effective_date=date(2026, 9, 23))
        await cache.get(item, load, effective_date=date(2026, 9, 24))
        await cache.invalidate(item)
        await cache.get(item, load, effective_date=date(2026, 9, 23))

    asyncio.run(run())
    assert calls == 3


def test_evicts_least_recently_used_snapshot(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(2)
    calls: dict[str, int] = {}

    def load(item: RepositorySnapshot):
        calls[item.sha] = calls.get(item.sha, 0) + 1
        return object()

    async def run() -> None:
        first = snapshot(tmp_path / "first", "1" * 40)
        second = snapshot(tmp_path / "second", "2" * 40)
        third = snapshot(tmp_path / "third", "3" * 40)
        await cache.get(first, load)
        await cache.get(second, load)
        await cache.get(first, load)
        await cache.get(third, load)
        await cache.get(second, load)

    asyncio.run(run())
    assert calls == {"1" * 40: 1, "2" * 40: 2, "3" * 40: 1}


def test_rejects_unique_miss_beyond_queue_depth(tmp_path: Path) -> None:
    cache = ParsedLedgerCache(8, max_queue_depth=1)
    started = threading.Event()
    release = threading.Event()

    def load(_snapshot: RepositorySnapshot):
        started.set()
        release.wait(timeout=2)
        return object()

    async def run() -> None:
        first = asyncio.create_task(
            cache.get(snapshot(tmp_path / "first", "1" * 40), load)
        )
        await asyncio.to_thread(started.wait, 2)
        try:
            await cache.get(snapshot(tmp_path / "second", "2" * 40), load)
        except ServiceError as error:
            assert error.status == 503
            assert str(error) == "Parsed ledger queue is full"
        else:
            raise AssertionError("unique cache miss should have been rejected")
        release.set()
        await first

    asyncio.run(run())

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    port: int
    gitea_url: str
    repositories_root: Path
    cache_root: Path
    parsed_ledger_cache_entries: int
    parsed_ledger_queue_depth: int
    materialized_snapshots_per_repo: int
    dependency_cache_entries: int
    cache_eviction_grace_seconds: int
    webhook_token: str | None


def load_settings() -> Settings:
    host = os.environ.get("GITEA_HOST_NAME", "gitea")
    port = int(os.environ.get("GITEA_HTTP_PORT", "3000"))
    protocol = os.environ.get("GITEA_PROTOCOL", "http")
    return Settings(
        port=int(os.environ.get("PORT", "8000")),
        gitea_url=f"{protocol}://{host}:{port}",
        repositories_root=Path(
            os.environ.get(
                "GITEA_REPOSITORIES_ROOT", "/gitea-data/git/repositories"
            )
        ),
        cache_root=Path(
            os.environ.get("PYTHON_LEDGER_CACHE_ROOT", "/var/cache/beancount-python")
        ),
        parsed_ledger_cache_entries=max(
            1, int(os.environ.get("PARSED_LEDGER_CACHE_ENTRIES", "8"))
        ),
        parsed_ledger_queue_depth=max(
            1, int(os.environ.get("PARSED_LEDGER_QUEUE_DEPTH", "32"))
        ),
        materialized_snapshots_per_repo=max(
            8, int(os.environ.get("MATERIALIZED_SNAPSHOTS_PER_REPO", "16"))
        ),
        dependency_cache_entries=max(
            1, int(os.environ.get("DEPENDENCY_CACHE_ENTRIES", "32"))
        ),
        cache_eviction_grace_seconds=max(
            0, int(os.environ.get("CACHE_EVICTION_GRACE_SECONDS", "300"))
        ),
        webhook_token=os.environ.get("WEBHOOK_TOKEN") or None,
    )


settings = load_settings()

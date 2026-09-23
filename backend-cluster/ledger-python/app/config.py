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
        webhook_token=os.environ.get("WEBHOOK_TOKEN") or None,
    )


settings = load_settings()

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI

from .admin import router as admin_router
from .collaborators import router as collaborators_router
from .entries import router as entries_router
from .errors import ServiceError, service_error_handler, success
from .files import router as files_router
from .journal import router as journal_router
from .keys import router as keys_router
from .ledgers import router as ledgers_router
from .legacy import router as legacy_router
from .repo import router as repo_router
from .reports import router as reports_router
from .shell import router as shell_router
from .tokens import router as tokens_router
from .users import router as users_router
from .webhooks import router as webhooks_router


app = FastAPI(title="Beancount Python Ledger", version="0.1.0")
app.add_exception_handler(ServiceError, service_error_handler)
app.include_router(admin_router)
app.include_router(users_router)
app.include_router(ledgers_router)
app.include_router(files_router)
app.include_router(entries_router)
app.include_router(reports_router)
app.include_router(shell_router)
app.include_router(journal_router)
app.include_router(legacy_router)
app.include_router(keys_router)
app.include_router(collaborators_router)
app.include_router(repo_router)
app.include_router(tokens_router)
app.include_router(webhooks_router)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return success(
        {
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "engine": "python-beancount",
        }
    )

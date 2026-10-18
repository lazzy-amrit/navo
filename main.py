"""InSQL backend application entry point.

Run with: ``uvicorn main:app --reload``
"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from Auth.google import router as google_router
from Auth.login import router as auth_router
from turso import service
from turso import turso as turso_client
from turso.errors import register_exception_handlers
from turso.models import create_tables
from turso.router import admin_router, customer_router, gateway_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    app.state.turso_platform = turso_client.get_platform_client()
    app.state.turso_sql = turso_client.get_sql_client()
    sync_task = asyncio.create_task(service.usage_sync_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task
        await turso_client.reset_clients()


def create_app() -> FastAPI:
    app = FastAPI(
        title="InSQL",
        description="Tiny hosted SQLite databases.",
        version="0.1.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(google_router)
    app.include_router(customer_router)
    app.include_router(gateway_router)
    app.include_router(admin_router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ultrademo_api import errors
from ultrademo_api.db import Database
from ultrademo_api.routers import client, internal, public
from ultrademo_api.security import KeyCache
from ultrademo_api.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url, settings.db_pool_min, settings.db_pool_max)
        await db.connect()
        app.state.db = db
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(
        title="ultrademo API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.key_cache = KeyCache(settings.api_key_cache_ttl_s)
    errors.install(app)
    app.include_router(client.router)
    app.include_router(public.router)
    app.include_router(internal.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        async with app.state.db.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}

    return app

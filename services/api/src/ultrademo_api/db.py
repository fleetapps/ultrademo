"""asyncpg pool with per-transaction tenant scoping.

Every tenant query runs inside `tenant(org_id)`, which switches to the non-owner `ultrademo_app` role
and sets `app.org_id` for the transaction only (`SET LOCAL` / `set_config(..., true)`), so RLS
policies in db/migrations filter every row. Pooled connections never leak one tenant's setting into
the next request because both reset at transaction end.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max, init=_init_connection
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database not connected")
        return self._pool

    @asynccontextmanager
    async def app(self) -> AsyncIterator[asyncpg.Connection]:
        """A transaction as `ultrademo_app` with no tenant set: only SECURITY DEFINER lookups work."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            yield conn

    @asynccontextmanager
    async def tenant(self, org_id: UUID) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
            yield conn

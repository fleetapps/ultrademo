"""Apply db/migrations/*.sql in order, once each, under an advisory lock.

Plain SQL files keep the DDL reviewable and identical to what docs/03 describes. Run with
`python -m ultrademo_api.migrate` before starting the api (the container entrypoint does this).
"""

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from ultrademo_api.settings import get_settings

_LOCK_ID = 0x756C7472  # "ultr"


def default_migrations_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "db" / "migrations"


async def migrate(dsn: str, directory: Path) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"]: r["sha256"] for r in await conn.fetch("SELECT name, sha256 FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text()
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise RuntimeError(f"{path.name} changed after it was applied; add a new migration")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (name, sha256) VALUES ($1, $2)", path.name, digest
                )
            applied.append(path.name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
        await conn.close()
    return applied


def main() -> None:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else default_migrations_dir()
    applied = asyncio.run(migrate(get_settings().database_url, directory))
    print("applied:", ", ".join(applied) if applied else "nothing new")


if __name__ == "__main__":
    main()

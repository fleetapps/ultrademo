"""Create a fresh database for the browser end-to-end test, migrate it and load e2e/spec.json.

    ULTRADEMO_TEST_ADMIN_DSN=postgresql://postgres@localhost:5432/postgres python e2e/prepare_db.py

Prints the database URL the api should use. The database is dropped and recreated each run.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from ultrademo_api.bootstrap import bootstrap
from ultrademo_api.migrate import default_migrations_dir, migrate

NAME = "ultrademo_e2e"


async def main() -> None:
    admin = os.environ["ULTRADEMO_TEST_ADMIN_DSN"]
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {NAME}")
    finally:
        await conn.close()
    # Same server and credentials, other database; any ?query (sslmode and the like) is kept.
    dsn = urlsplit(admin)._replace(path=f"/{NAME}").geturl()
    await migrate(dsn, default_migrations_dir())
    spec = json.loads((Path(__file__).parent / "spec.json").read_text())
    await bootstrap(dsn, spec)
    print(dsn)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)

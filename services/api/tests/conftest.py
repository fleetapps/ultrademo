import os
import uuid

import asyncpg
import httpx
import pytest
from ultrademo_api.app import create_app
from ultrademo_api.bootstrap import bootstrap
from ultrademo_api.db import Database
from ultrademo_api.migrate import default_migrations_dir, migrate
from ultrademo_api.settings import Settings

ADMIN_DSN = os.environ.get(
    "ULTRADEMO_TEST_ADMIN_DSN", "postgresql://postgres@localhost:5432/postgres"
)


def spec(slug: str, status: str = "published") -> dict:
    return {
        "organization": {"slug": slug, "name": slug.title()},
        "product": {
            "name": f"{slug} CRM",
            "base_url": f"https://app.{slug}.example",
            "allowed_domains": [f"app.{slug}.example"],
        },
        "agent": {"name": "Ava", "system_prompt": f"You demo the {slug} CRM."},
        "launch_config": {
            "slug": f"{slug}-demo",
            "name": f"{slug} demo",
            "status": status,
            "ctas": [{"kind": "book", "label": "Book a call", "url": "https://cal.example/x"}],
        },
    }


@pytest.fixture(scope="session")
async def dsn():
    name = f"ud_test_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    test_dsn = ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    await migrate(test_dsn, default_migrations_dir())
    yield test_dsn
    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


@pytest.fixture(scope="session")
async def orgs(dsn):
    return {
        "acme": await bootstrap(dsn, spec("acme")),
        "globex": await bootstrap(dsn, spec("globex")),
        "draft": await bootstrap(dsn, spec("initech", status="draft")),
    }


@pytest.fixture(scope="session")
def settings(dsn) -> Settings:
    return Settings(
        database_url=dsn, internal_token="test-internal", public_base_url="https://ud.example"
    )


@pytest.fixture(scope="session")
async def client(settings, orgs):
    app = create_app(settings)
    db = Database(settings.database_url)
    await db.connect()
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await db.close()

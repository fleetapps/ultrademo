"""Onboard a design partner from one JSON file until the builder UI exists.

    python -m ultrademo_api.bootstrap examples/acme.json

Creates the org (or reuses it by slug), its product, an agent with version 1, a launch config and an
API key. Prints the key once; only its argon2 hash is stored.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg

from ultrademo_api.db import _init_connection
from ultrademo_api.security import generate_api_key
from ultrademo_api.settings import get_settings

DEFAULT_SCOPES = ["launch_configs:read", "context_links:write", "sessions:read"]


async def bootstrap(dsn: str, spec: dict[str, Any]) -> dict[str, str]:
    conn = await asyncpg.connect(dsn)
    await _init_connection(conn)
    try:
        async with conn.transaction():
            org = spec["organization"]
            org_id = await conn.fetchval(
                "INSERT INTO organizations (slug, name, region) VALUES ($1, $2, $3)"
                " ON CONFLICT (slug) DO UPDATE SET name = excluded.name RETURNING id",
                org["slug"], org["name"], org.get("region", "us"),
            )
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
            p = spec["product"]
            product_id = await conn.fetchval(
                "INSERT INTO products (org_id, name, base_url, allowed_domains)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                org_id, p["name"], p["base_url"], p["allowed_domains"],
            )
            a = spec["agent"]
            agent_id = await conn.fetchval(
                "INSERT INTO agents (org_id, product_id, name) VALUES ($1, $2, $3) RETURNING id",
                org_id, product_id, a["name"],
            )
            av_id = await conn.fetchval(
                "INSERT INTO agent_versions (org_id, agent_id, version, system_prompt, voice, policy, model)"
                " VALUES ($1, $2, 1, $3, $4, $5, $6) RETURNING id",
                org_id, agent_id, a["system_prompt"], a.get("voice", {}), a.get("policy", {}),
                a.get("model", {}),
            )
            lc = spec["launch_config"]
            await conn.execute(
                "INSERT INTO launch_configs (org_id, slug, name, description, status, agent_version_id,"
                " ctas, embed_origins) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                org_id, lc["slug"], lc["name"], lc.get("description", ""),
                lc.get("status", "published"), av_id, lc.get("ctas", []), lc.get("embed_origins", []),
            )
            key = generate_api_key()
            await conn.execute(
                "INSERT INTO api_keys (org_id, name, prefix, key_hash, scopes) VALUES ($1, $2, $3, $4, $5)",
                org_id, "bootstrap", key.prefix, key.key_hash, spec.get("api_key_scopes", DEFAULT_SCOPES),
            )
    finally:
        await conn.close()
    return {"org_id": str(org_id), "launch_config": lc["slug"], "api_key": key.plaintext}


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text())
    out = asyncio.run(bootstrap(get_settings().database_url, spec))
    print(json.dumps(out, indent=2))
    print("Store the api_key now; it cannot be shown again.", file=sys.stderr)


if __name__ == "__main__":
    main()

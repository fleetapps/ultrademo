from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from ultrademo_api.db import Database
from ultrademo_api.errors import ApiError
from ultrademo_api.security import KeyCache, Principal, constant_time_equals, parse_prefix, verify_api_key
from ultrademo_api.settings import Settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "unauthorized", "Missing bearer token")
    return token.strip()


async def get_principal(request: Request, db: Annotated[Database, Depends(get_db)]) -> Principal:
    token = _bearer(request)
    cache: KeyCache = request.app.state.key_cache
    cached = cache.get(token)
    if cached is not None:
        return cached
    prefix = parse_prefix(token)
    if prefix is None:
        raise ApiError(401, "unauthorized", "Invalid API key")
    async with db.app() as conn:
        row = await conn.fetchrow("SELECT * FROM auth_api_key($1)", prefix)
    if row is None or not verify_api_key(token, row["key_hash"]):
        raise ApiError(401, "unauthorized", "Invalid API key")
    principal = Principal(org_id=row["org_id"], key_id=row["key_id"], scopes=frozenset(row["scopes"]))
    async with db.tenant(principal.org_id) as conn:
        await conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = $1", principal.key_id)
    cache.put(token, principal)
    return principal


def require(scope: str):
    async def _check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.can(scope):
            raise ApiError(403, "insufficient_scope", f"This key needs the {scope} scope")
        return principal

    return _check


async def require_internal(
    request: Request, settings: Annotated[Settings, Depends(get_settings_dep)]
) -> None:
    if not constant_time_equals(_bearer(request), settings.internal_token.get_secret_value()):
        raise ApiError(401, "unauthorized", "Invalid internal token")


async def session_org(session_id: UUID, db: Annotated[Database, Depends(get_db)]) -> UUID:
    async with db.app() as conn:
        row = await conn.fetchrow("SELECT org_id FROM resolve_session($1)", session_id)
    if row is None:
        raise ApiError(404, "not_found", "Session not found")
    return row["org_id"]

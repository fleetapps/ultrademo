"""Unauthenticated endpoints the demo player calls from the browser.

A viewer opens `/d/<slug>` (optionally `?t=<context-link token>`); the player POSTs here, gets a
LiveKit token for a fresh room, and the session agent is dispatched into that room by the token.
"""

import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ultrademo_api.db import Database
from ultrademo_api.deps import get_db, get_settings_dep
from ultrademo_api.errors import ApiError
from ultrademo_api.livekit_tokens import viewer_token
from ultrademo_api.settings import Settings

router = APIRouter(prefix="/v1/public", tags=["public"])


class StartSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=63)
    token: str | None = Field(default=None, max_length=64)
    params: dict[str, str] = Field(default_factory=dict, max_length=20)
    locale: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    visitor_id: str | None = Field(default=None, max_length=64)
    test: bool = False


class StartSessionOut(BaseModel):
    session_id: UUID
    livekit_url: str
    token: str
    launch_config: dict[str, Any]


@router.post("/sessions", status_code=201, response_model=StartSessionOut)
async def start_session(
    body: StartSessionIn,
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> StartSessionOut:
    async with db.app() as conn:
        lc_ref = await conn.fetchrow("SELECT * FROM resolve_launch_config($1)", body.slug)
        link_ref = (
            await conn.fetchrow("SELECT * FROM resolve_context_link($1)", body.token)
            if body.token
            else None
        )
    if lc_ref is None:
        raise ApiError(404, "not_found", "This demo does not exist")
    allowed_status = {"published", "testing"} if body.test else {"published"}
    if lc_ref["status"] not in allowed_status:
        raise ApiError(404, "not_found", "This demo is not live")
    if link_ref is not None and link_ref["launch_config_id"] != lc_ref["launch_config_id"]:
        link_ref = None  # a token for another demo is ignored, not an error the viewer can act on

    org_id = lc_ref["org_id"]
    room = f"ud_{secrets.token_urlsafe(12)}"
    async with db.tenant(org_id) as conn:
        lc = await conn.fetchrow(
            "SELECT id, slug, name, agent_version_id, ctas, branding, embed_origins, form_schema"
            " FROM launch_configs WHERE id = $1",
            lc_ref["launch_config_id"],
        )
        origin = request.headers.get("origin")
        if origin and lc["embed_origins"] and origin not in lc["embed_origins"]:
            player_origin = settings.public_base_url.rstrip("/")
            if origin != player_origin:
                raise ApiError(403, "origin_not_allowed", "This demo cannot be embedded here")
        session_id = await conn.fetchval(
            "INSERT INTO sessions (org_id, launch_config_id, agent_version_id, context_link_id, type,"
            " params, locale, visitor_id, livekit_room)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
            org_id,
            lc["id"],
            lc["agent_version_id"],
            link_ref["context_link_id"] if link_ref else None,
            "test" if body.test else "prod",
            body.params,
            body.locale,
            body.visitor_id,
            room,
        )
        await conn.execute(
            "INSERT INTO outbox (org_id, topic, payload) VALUES ($1, 'session.created', $2)",
            org_id,
            {"session_id": str(session_id), "launch_config": lc["slug"]},
        )

    token = viewer_token(
        settings, room=room, identity=f"viewer_{session_id}", session_id=str(session_id)
    )
    return StartSessionOut(
        session_id=session_id,
        livekit_url=settings.livekit_url,
        token=token,
        launch_config={
            "slug": lc["slug"],
            "name": lc["name"],
            "ctas": lc["ctas"],
            "branding": lc["branding"],
            "form_schema": lc["form_schema"],
        },
    )

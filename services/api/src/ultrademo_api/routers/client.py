"""Client API v1 (docs/04 §3): what a customer's backend, CRM automation or MCP server calls.

Field names follow Supersonik's published schema where sensible (`demo_url_slug`, `is_valid`,
`duration_seconds`, `participant_joined`) so an existing integration can switch with a base-URL
change. List endpoints return no PII.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ultrademo_api.db import Database
from ultrademo_api.deps import get_db, get_settings_dep, require
from ultrademo_api.errors import ApiError
from ultrademo_api.pagination import decode_cursor, encode_cursor
from ultrademo_api.security import Principal, random_token
from ultrademo_api.settings import Settings

router = APIRouter(prefix="/v1/client", tags=["client"])

MAX_CONTEXT_BYTES = 32 * 1024
MAX_CONTEXT_DEPTH = 5


def _depth(value: Any, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level, *(_depth(v, level + 1) for v in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(v, level + 1) for v in value)])
    return level - 1


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    company: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    calendar_url: str | None = Field(default=None, max_length=2048)


class Brief(BaseModel):
    """What the rep wants the agent to emphasise; also what the rep sees after the call."""

    model_config = ConfigDict(extra="forbid")
    emphasis: list[str] = Field(default_factory=list, max_length=10)
    questions: list[str] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=4000)


class ContextLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    launch_config_slug: str
    context: dict[str, Any] = Field(default_factory=dict)
    recipient: Person | None = None
    sender: Person | None = None
    brief: Brief | None = None
    expires_at: datetime | None = None

    @field_validator("context")
    @classmethod
    def _bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context must be at most {MAX_CONTEXT_BYTES} bytes as JSON")
        if _depth(v) > MAX_CONTEXT_DEPTH:
            raise ValueError(f"context must nest at most {MAX_CONTEXT_DEPTH} levels")
        return v


class DemoIn(BaseModel):
    """Supersonik-compatible alias body."""

    model_config = ConfigDict(extra="forbid")
    demo_url_slug: str
    context: dict[str, Any] = Field(default_factory=dict)
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    company: str | None = None
    phone: str | None = None


class ContextLinkOut(BaseModel):
    id: UUID
    token: str
    url: str


class LaunchConfigOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    status: str
    url: str


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


class SessionListItem(BaseModel):
    id: UUID
    type: str
    status: str
    start_time: datetime | None
    end_time: datetime | None
    duration_seconds: int | None
    participant_joined: bool
    is_valid: bool | None
    launch_config: str
    details_url: str


class TranscriptMessageOut(BaseModel):
    seq: int
    role: str
    speaker_name: str | None
    content: str
    source: str
    lang: str | None
    interrupted: bool
    started_at: datetime


class SessionDetail(SessionListItem):
    locale: str
    params: dict[str, Any]
    form: dict[str, Any] | None
    context: dict[str, Any] | None
    summary: str | None
    end_reason: str | None
    cost_usd: float
    actions: int


def _demo_url(settings: Settings, slug: str, token: str | None = None) -> str:
    base = f"{settings.public_base_url.rstrip('/')}/d/{slug}"
    return f"{base}?t={token}" if token else base


def _is_valid(validity: str) -> bool | None:
    return {"valid": True, "invalid": False}.get(validity)


@router.get("/launch-configs", response_model=Page[LaunchConfigOut])
async def list_launch_configs(
    principal: Annotated[Principal, Depends(require("launch_configs:read"))],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    status: Literal["published", "testing", "draft", "archived"] | None = None,
) -> Page[LaunchConfigOut]:
    async with db.tenant(principal.org_id) as conn:
        rows = await conn.fetch(
            "SELECT id, slug, name, description, status FROM launch_configs"
            " WHERE ($1::text IS NULL OR status = $1) ORDER BY name",
            status,
        )
    return Page(
        items=[LaunchConfigOut(**dict(r), url=_demo_url(settings, r["slug"])) for r in rows],
        next_cursor=None,
    )


async def _create_context_link(
    db: Database, settings: Settings, principal: Principal, body: ContextLinkIn
) -> ContextLinkOut:
    token = random_token(16)
    async with db.tenant(principal.org_id) as conn:
        lc = await conn.fetchrow(
            "SELECT id, slug, status FROM launch_configs WHERE slug = $1", body.launch_config_slug
        )
        if lc is None:
            raise ApiError(
                404, "not_found", f"No launch config with slug {body.launch_config_slug!r}"
            )
        if lc["status"] == "archived":
            raise ApiError(409, "archived", "That launch config is archived")
        link_id = await conn.fetchval(
            "INSERT INTO context_links"
            " (org_id, launch_config_id, token, context, recipient, sender, brief, source, created_by, expires_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, 'api', $8, $9) RETURNING id",
            principal.org_id,
            lc["id"],
            token,
            body.context,
            body.recipient.model_dump(exclude_none=True) if body.recipient else None,
            body.sender.model_dump(exclude_none=True) if body.sender else None,
            body.brief.model_dump(exclude_none=True) if body.brief else None,
            f"api_key:{principal.key_id}",
            body.expires_at,
        )
        await conn.execute(
            "INSERT INTO outbox (org_id, topic, payload) VALUES ($1, 'context_link.created', $2)",
            principal.org_id,
            {"context_link_id": str(link_id), "launch_config": lc["slug"]},
        )
    return ContextLinkOut(id=link_id, token=token, url=_demo_url(settings, lc["slug"], token))


@router.post("/context-links", status_code=201, response_model=ContextLinkOut)
async def create_context_link(
    body: ContextLinkIn,
    principal: Annotated[Principal, Depends(require("context_links:write"))],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ContextLinkOut:
    return await _create_context_link(db, settings, principal, body)


@router.post("/demos", status_code=201, response_model=ContextLinkOut)
async def create_demo(
    body: DemoIn,
    principal: Annotated[Principal, Depends(require("context_links:write"))],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ContextLinkOut:
    person = Person(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        company=body.company,
        phone=body.phone,
    )
    has_person = any(v is not None for v in person.model_dump().values())
    link = ContextLinkIn(
        launch_config_slug=body.demo_url_slug,
        context=body.context,
        recipient=person if has_person else None,
    )
    return await _create_context_link(db, settings, principal, link)


_SESSION_COLS = (
    "s.id, s.type, s.status, s.started_at, s.ended_at, s.duration_s, s.participant_joined,"
    " s.validity, s.created_at, lc.slug AS launch_config"
)


def _list_item(settings: Settings, r: Any) -> SessionListItem:
    return SessionListItem(
        id=r["id"],
        type=r["type"],
        status=r["status"],
        start_time=r["started_at"],
        end_time=r["ended_at"],
        duration_seconds=r["duration_s"],
        participant_joined=r["participant_joined"],
        is_valid=_is_valid(r["validity"]),
        launch_config=r["launch_config"],
        details_url=f"{settings.public_base_url.rstrip('/')}/app/sessions/{r['id']}",
    )


@router.get("/sessions", response_model=Page[SessionListItem])
async def list_sessions(
    principal: Annotated[Principal, Depends(require("sessions:read"))],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    type: Literal["prod", "test", "eval"] | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
) -> Page[SessionListItem]:
    after_ts, after_id = decode_cursor(cursor) if cursor else (None, None)
    async with db.tenant(principal.org_id) as conn:
        rows = await conn.fetch(
            # _SESSION_COLS is a module constant; every value is a bind parameter.
            f"SELECT {_SESSION_COLS} FROM sessions s JOIN launch_configs lc ON lc.id = s.launch_config_id"  # noqa: S608
            " WHERE ($1::text IS NULL OR s.type = $1)"
            "   AND ($2::timestamptz IS NULL OR s.started_at >= $2)"
            "   AND ($3::timestamptz IS NULL OR s.started_at < $3)"
            "   AND ($4::timestamptz IS NULL OR (s.created_at, s.id) < ($4, $5::uuid))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT $6",
            type,
            started_from,
            started_to,
            after_ts,
            after_id,
            limit + 1,
        )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page(
        items=[_list_item(settings, r) for r in rows],
        next_cursor=encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more else None,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: UUID,
    principal: Annotated[Principal, Depends(require("sessions:read"))],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionDetail:
    async with db.tenant(principal.org_id) as conn:
        r = await conn.fetchrow(
            f"SELECT {_SESSION_COLS}, s.locale, s.params, s.form, s.summary, s.end_reason,"  # noqa: S608
            " cl.context,"
            " (SELECT coalesce(sum(usd), 0) FROM cost_ledger c WHERE c.session_id = s.id) AS cost_usd,"
            " (SELECT count(*) FROM agent_actions a WHERE a.session_id = s.id) AS actions"
            " FROM sessions s JOIN launch_configs lc ON lc.id = s.launch_config_id"
            " LEFT JOIN context_links cl ON cl.id = s.context_link_id WHERE s.id = $1",
            session_id,
        )
    if r is None:
        raise ApiError(404, "not_found", "Session not found")
    return SessionDetail(
        **_list_item(settings, r).model_dump(),
        locale=r["locale"],
        params=r["params"],
        form=r["form"],
        context=r["context"],
        summary=r["summary"],
        end_reason=r["end_reason"],
        cost_usd=float(r["cost_usd"]),
        actions=r["actions"],
    )


@router.get("/sessions/{session_id}/transcript", response_model=list[TranscriptMessageOut])
async def get_transcript(
    session_id: UUID,
    principal: Annotated[Principal, Depends(require("sessions:read"))],
    db: Annotated[Database, Depends(get_db)],
) -> list[TranscriptMessageOut]:
    async with db.tenant(principal.org_id) as conn:
        exists = await conn.fetchval("SELECT 1 FROM sessions WHERE id = $1", session_id)
        if not exists:
            raise ApiError(404, "not_found", "Session not found")
        rows = await conn.fetch(
            "SELECT seq, role, speaker_name, content, source, lang, interrupted, started_at"
            " FROM transcript_messages WHERE session_id = $1 ORDER BY seq",
            session_id,
        )
    return [TranscriptMessageOut(**dict(r)) for r in rows]

"""Unauthenticated endpoints the demo player calls from the browser.

A viewer opens `/d/<slug>` (optionally `?t=<context-link token>`). The player reads the public part
of the launch config, POSTs here to start a session, gets a LiveKit token for a fresh room, and the
session agent is dispatched into that room by the token. The start response also carries a
`receipt`, a credential scoped to that one session, which the player uses to report CTA clicks and
post-call feedback after the room is gone.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from ultrademo_api.db import Database
from ultrademo_api.deps import get_db, get_settings_dep
from ultrademo_api.errors import ApiError
from ultrademo_api.livekit_tokens import viewer_token
from ultrademo_api.security import constant_time_equals, session_receipt
from ultrademo_api.settings import Settings

router = APIRouter(prefix="/v1/public", tags=["public"])

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_EVENTS_PER_SESSION = 50  # CTA clicks per session
MAX_FEEDBACK_EDITS = 5  # a viewer may change their rating a few times


class FormField(BaseModel):
    """One pre-call form field. `launch_configs.form_schema` is `{"fields": [FormField, ...]}`."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=80)
    type: Literal["text", "email", "tel", "select"] = "text"
    required: bool = False
    options: list[str] | None = Field(default=None, max_length=30)


class FormSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fields: list[FormField] = Field(default_factory=list, max_length=10)


def _form_schema(raw: Any) -> FormSchema | None:
    if not raw:
        return None
    try:
        return FormSchema.model_validate(raw)
    except ValidationError:
        # A malformed schema is an authoring bug; never block the viewer over it.
        return None


def _check_form(schema: FormSchema | None, form: dict[str, str]) -> dict[str, str]:
    fields = {f.name: f for f in schema.fields} if schema else {}
    unknown = sorted(set(form) - set(fields))
    if unknown:
        raise ApiError(422, "invalid_form", f"Unknown form fields: {', '.join(unknown)}")
    clean: dict[str, str] = {}
    for name, f in fields.items():
        value = form.get(name, "").strip()
        if not value:
            if f.required:
                raise ApiError(422, "invalid_form", f"{f.label} is required")
            continue
        if f.type == "email" and not _EMAIL.match(value):
            raise ApiError(422, "invalid_form", f"{f.label} must be an email address")
        if f.type == "select" and f.options and value not in f.options:
            raise ApiError(422, "invalid_form", f"{f.label} must be one of the listed options")
        clean[name] = value
    return clean


def _public_ctas(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i, c in enumerate(raw or []):
        if not isinstance(c, dict) or not c.get("label"):
            continue
        url = c.get("url")
        out.append(
            {
                # Capped to what a click report may carry (CtaClickedIn.cta_id).
                "id": str(c.get("id") or f"{c.get('kind', 'link')}-{i}")[:80],
                "kind": str(c.get("kind", "link")),
                "label": str(c["label"])[:80],
                **({"url": url} if isinstance(url, str) and url.startswith("https://") else {}),
            }
        )
    return out


def _public_branding(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    if isinstance(raw.get("accent"), str) and _HEX.match(raw["accent"]):
        out["accent"] = raw["accent"]
    if isinstance(raw.get("logo_url"), str) and raw["logo_url"].startswith("https://"):
        out["logo_url"] = raw["logo_url"]
    return out


class LaunchConfigPublic(BaseModel):
    slug: str
    name: str
    description: str
    status: str
    agent_name: str
    product_name: str
    ctas: list[dict[str, str]]
    branding: dict[str, str]
    form: FormSchema | None
    embed_origins: list[str]
    max_duration_s: int


@router.get("/launch-configs/{slug}", response_model=LaunchConfigPublic)
async def get_launch_config(
    slug: str, db: Annotated[Database, Depends(get_db)], test: bool = False
) -> LaunchConfigPublic:
    async with db.app() as conn:
        ref = await conn.fetchrow("SELECT * FROM resolve_launch_config($1)", slug)
    if ref is None or ref["status"] not in ({"published", "testing"} if test else {"published"}):
        raise ApiError(404, "not_found", "This demo does not exist or is not live")
    async with db.tenant(ref["org_id"]) as conn:
        r = await conn.fetchrow(
            "SELECT lc.slug, lc.name, lc.description, lc.status, lc.ctas, lc.branding,"
            " lc.form_schema, lc.embed_origins, lc.max_duration_s,"
            " a.name AS agent_name, p.name AS product_name"
            " FROM launch_configs lc"
            " JOIN agent_versions av ON av.id = lc.agent_version_id"
            " JOIN agents a ON a.id = av.agent_id"
            " JOIN products p ON p.id = a.product_id"
            " WHERE lc.id = $1",
            ref["launch_config_id"],
        )
    return LaunchConfigPublic(
        slug=r["slug"],
        name=r["name"],
        description=r["description"],
        status=r["status"],
        agent_name=r["agent_name"],
        product_name=r["product_name"],
        ctas=_public_ctas(r["ctas"]),
        branding=_public_branding(r["branding"]),
        form=_form_schema(r["form_schema"]),
        embed_origins=list(r["embed_origins"]),
        max_duration_s=r["max_duration_s"],
    )


class StartSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=63)
    token: str | None = Field(default=None, max_length=64)
    params: dict[str, str] = Field(default_factory=dict, max_length=20)
    form: dict[str, Annotated[str, Field(max_length=500)]] = Field(
        default_factory=dict, max_length=10
    )
    locale: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    visitor_id: str | None = Field(default=None, max_length=64)
    test: bool = False


class StartSessionOut(BaseModel):
    session_id: UUID
    livekit_url: str
    token: str
    receipt: str
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
        form = _check_form(_form_schema(lc["form_schema"]), body.form)
        session_id = await conn.fetchval(
            "INSERT INTO sessions (org_id, launch_config_id, agent_version_id, context_link_id, type,"
            " params, form, locale, visitor_id, livekit_room)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            org_id,
            lc["id"],
            lc["agent_version_id"],
            link_ref["context_link_id"] if link_ref else None,
            "test" if body.test else "prod",
            body.params,
            form or None,
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
        receipt=session_receipt(settings.receipt_secret.get_secret_value(), session_id),
        launch_config={
            "slug": lc["slug"],
            "name": lc["name"],
            "ctas": _public_ctas(lc["ctas"]),
            "branding": _public_branding(lc["branding"]),
        },
    )


class CtaClickedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["cta.clicked"]
    cta_id: str = Field(min_length=1, max_length=80)
    # Accepted for older players and ignored: the stored label comes from the launch config.
    label: str = Field(default="", max_length=80)
    phase: Literal["in_call", "post_call"] = "in_call"


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["feedback"]
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


class SessionEventIn(RootModel[Annotated[CtaClickedIn | FeedbackIn, Field(discriminator="type")]]):
    """`{"type": "cta.clicked", ...}` or `{"type": "feedback", ...}`."""


@router.post("/sessions/{session_id}/events", status_code=204)
async def report_event(
    session_id: UUID,
    body: SessionEventIn,
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    expected = session_receipt(settings.receipt_secret.get_secret_value(), session_id)
    if scheme.lower() != "bearer" or not constant_time_equals(presented.strip(), expected):
        raise ApiError(401, "unauthorized", "Invalid session receipt")

    async with db.app() as conn:
        ref = await conn.fetchrow("SELECT * FROM resolve_session($1)", session_id)
    if ref is None:
        raise ApiError(404, "not_found", "Session not found")
    org_id = ref["org_id"]
    event = body.root
    async with db.tenant(org_id) as conn:
        # Locking the session row serializes this session's reports, so the caps below hold
        # under concurrent requests.
        s = await conn.fetchrow(
            "SELECT s.started_at, lc.ctas FROM sessions s"
            " JOIN launch_configs lc ON lc.id = s.launch_config_id"
            " WHERE s.id = $1 FOR UPDATE OF s",
            session_id,
        )
        # Only a call that actually started can report, for a day after it started.
        if s is None or s["started_at"] is None:
            raise ApiError(409, "not_started", "This session has not started")
        if datetime.now(UTC) - s["started_at"] > timedelta(seconds=settings.receipt_ttl_s):
            raise ApiError(410, "expired", "This session no longer accepts events")
        if isinstance(event, FeedbackIn):
            payload: dict[str, Any] = event.model_dump(exclude={"type"})
            prev = await conn.fetchval(
                "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'feedback'",
                session_id,
            )
            edits = int((prev or {}).get("edits", 0)) + (1 if prev else 0)
            if edits > MAX_FEEDBACK_EDITS:
                raise ApiError(429, "too_many_events", "This session has reported enough events")
            if prev and {k: prev.get(k) for k in payload} == payload:
                return  # the same answer again: nothing new for the outbox
            await conn.execute(
                "INSERT INTO session_events (org_id, session_id, type, payload)"
                " VALUES ($1, $2, 'feedback', $3)"
                " ON CONFLICT (session_id) WHERE type = 'feedback'"
                " DO UPDATE SET payload = EXCLUDED.payload, created_at = now()",
                org_id,
                session_id,
                {**payload, "edits": edits},
            )
            topic = "session.feedback"
        else:
            # The click must name a CTA of this launch config; what is stored comes from the
            # config, never from the request, so reports can't invent conversions.
            cta = next((c for c in _public_ctas(s["ctas"]) if c["id"] == event.cta_id), None)
            if cta is None:
                raise ApiError(422, "unknown_cta", "No such call to action")
            count = await conn.fetchval(
                "SELECT count(*) FROM session_events WHERE session_id = $1 AND type = 'cta.clicked'",
                session_id,
            )
            if count >= MAX_EVENTS_PER_SESSION:
                raise ApiError(429, "too_many_events", "This session has reported enough events")
            payload = {
                "cta_id": cta["id"],
                "kind": cta["kind"],
                "label": cta["label"],
                "url": cta.get("url"),
                "phase": event.phase,
            }
            await conn.execute(
                "INSERT INTO session_events (org_id, session_id, type, payload)"
                " VALUES ($1, $2, 'cta.clicked', $3)",
                org_id,
                session_id,
                payload,
            )
            topic = "cta.clicked"
        await conn.execute(
            "INSERT INTO outbox (org_id, topic, payload) VALUES ($1, $2, $3)",
            org_id,
            topic,
            {"session_id": str(session_id), **payload},
        )

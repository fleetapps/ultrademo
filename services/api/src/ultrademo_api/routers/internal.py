"""Service-to-service endpoints the session agent calls (shared internal token, never a browser).

The agent is stateless between jobs: it fetches everything it needs for a session here, then
streams transcript lines, actions and cost back as the call happens.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ultrademo_api.db import Database
from ultrademo_api.deps import get_db, require_internal, session_org
from ultrademo_api.errors import ApiError

router = APIRouter(
    prefix="/v1/internal/sessions/{session_id}",
    tags=["internal"],
    dependencies=[Depends(require_internal)],
)


class SessionContext(BaseModel):
    session_id: UUID
    org_id: UUID
    type: str
    locale: str
    livekit_room: str
    max_duration_s: int
    agent_version: dict[str, Any]
    product: dict[str, Any]
    launch_config: dict[str, Any]
    params: dict[str, Any]
    form: dict[str, Any] | None
    context_link: dict[str, Any] | None


@router.get("/context", response_model=SessionContext)
async def get_context(
    session_id: UUID,
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> SessionContext:
    async with db.tenant(org_id) as conn:
        r = await conn.fetchrow(
            "SELECT s.id, s.type, s.locale, s.livekit_room, s.params, s.form,"
            " lc.slug, lc.name AS lc_name, lc.ctas, lc.max_duration_s,"
            " av.id AS av_id, av.version, av.system_prompt, av.voice, av.policy, av.model,"
            " p.name AS product_name, p.base_url, p.allowed_domains,"
            " cl.context, cl.recipient, cl.sender, cl.brief"
            " FROM sessions s"
            " JOIN launch_configs lc ON lc.id = s.launch_config_id"
            " JOIN agent_versions av ON av.id = s.agent_version_id"
            " JOIN agents a ON a.id = av.agent_id"
            " JOIN products p ON p.id = a.product_id"
            " LEFT JOIN context_links cl ON cl.id = s.context_link_id"
            " WHERE s.id = $1",
            session_id,
        )
    if r is None:
        raise ApiError(404, "not_found", "Session not found")
    link = None
    if r["context"] is not None:
        link = {k: r[k] for k in ("context", "recipient", "sender", "brief")}
    return SessionContext(
        session_id=r["id"],
        org_id=org_id,
        type=r["type"],
        locale=r["locale"],
        livekit_room=r["livekit_room"],
        max_duration_s=r["max_duration_s"],
        agent_version={
            "id": str(r["av_id"]),
            "version": r["version"],
            "system_prompt": r["system_prompt"],
            "voice": r["voice"],
            "policy": r["policy"],
            "model": r["model"],
        },
        product={
            "name": r["product_name"],
            "base_url": r["base_url"],
            "allowed_domains": r["allowed_domains"],
        },
        launch_config={"slug": r["slug"], "name": r["lc_name"], "ctas": r["ctas"]},
        params=r["params"],
        form=r["form"],
        context_link=link,
    )


@router.post("/started", status_code=204)
async def mark_started(
    session_id: UUID,
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    async with db.tenant(org_id) as conn:
        updated = await conn.fetchval(
            "UPDATE sessions SET status = 'live', started_at = coalesce(started_at, now()),"
            " participant_joined = true WHERE id = $1 AND status = 'created' RETURNING id",
            session_id,
        )
        if updated:
            await conn.execute(
                "INSERT INTO outbox (org_id, topic, payload) VALUES ($1, 'session.started', $2)",
                org_id,
                {"session_id": str(session_id)},
            )


class TranscriptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=0)
    role: Literal["agent", "participant", "human_rep", "system"]
    content: str = Field(max_length=20_000)
    source: Literal["voice", "chat"] = "voice"
    speaker_name: str | None = None
    lang: str | None = None
    interrupted: bool = False
    started_at: datetime
    ended_at: datetime | None = None


@router.post("/transcript", status_code=204)
async def append_transcript(
    session_id: UUID,
    lines: list[TranscriptLine],
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    async with db.tenant(org_id) as conn:
        # Idempotent on (session_id, seq): the agent retries a batch after a network error.
        await conn.executemany(
            "INSERT INTO transcript_messages (org_id, session_id, seq, role, speaker_name, content,"
            " source, lang, interrupted, started_at, ended_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)"
            " ON CONFLICT (session_id, seq) DO NOTHING",
            [
                (
                    org_id,
                    session_id,
                    x.seq,
                    x.role,
                    x.speaker_name,
                    x.content,
                    x.source,
                    x.lang,
                    x.interrupted,
                    x.started_at,
                    x.ended_at,
                )
                for x in lines
            ],
        )


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=0)
    tool: str
    args: dict[str, Any]
    status: Literal["ok", "needs_confirmation", "blocked", "error"]
    result_summary: str = Field(max_length=2000)
    policy_class: Literal["allowed", "confirm", "blocked"] = "allowed"
    element: dict[str, Any] | None = None
    latency_ms: int = Field(ge=0)


@router.post("/actions", status_code=204)
async def append_actions(
    session_id: UUID,
    actions: list[ActionIn],
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    async with db.tenant(org_id) as conn:
        await conn.executemany(
            "INSERT INTO agent_actions (org_id, session_id, seq, tool, args, status, result_summary,"
            " policy_class, element, latency_ms) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
            " ON CONFLICT (session_id, seq) DO NOTHING",
            [
                (
                    org_id,
                    session_id,
                    a.seq,
                    a.tool,
                    a.args,
                    a.status,
                    a.result_summary,
                    a.policy_class,
                    a.element,
                    a.latency_ms,
                )
                for a in actions
            ],
        )


class CostItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: Literal[
        "llm_in",
        "llm_out",
        "llm_cache_read",
        "llm_cache_write",
        "stt_sec",
        "tts_chars",
        "sandbox_sec",
        "livekit_agent_min",
        "livekit_participant_min",
        "egress_gb",
    ]
    qty: Decimal
    usd: Decimal
    rate_card_version: str


@router.post("/cost", status_code=204)
async def append_cost(
    session_id: UUID,
    items: list[CostItem],
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    async with db.tenant(org_id) as conn:
        await conn.executemany(
            "INSERT INTO cost_ledger (org_id, session_id, item, qty, usd, rate_card_version)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            [(org_id, session_id, c.item, c.qty, c.usd, c.rate_card_version) for c in items],
        )


class EndIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["agent_ended", "viewer_left", "max_duration", "error", "handoff"]
    summary: str | None = Field(default=None, max_length=4000)
    participant_turns: int = Field(default=0, ge=0)


@router.post("/ended", status_code=204)
async def mark_ended(
    session_id: UUID,
    body: EndIn,
    org_id: Annotated[UUID, Depends(session_org)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    # A session counts as valid once the viewer actually spoke twice; the insights pipeline can
    # refine this later. Mirrors Supersonik's `is_valid` without a model call on the hot path.
    validity = "valid" if body.participant_turns >= 2 else "invalid"
    async with db.tenant(org_id) as conn:
        updated = await conn.fetchval(
            "UPDATE sessions SET status = CASE WHEN $2 = 'error' THEN 'failed' ELSE 'ended' END,"
            " ended_at = now(), end_reason = $2, summary = $3, validity = $4"
            " WHERE id = $1 AND status IN ('created', 'live') RETURNING id",
            session_id,
            body.reason,
            body.summary,
            validity,
        )
        if updated:
            await conn.execute(
                "INSERT INTO outbox (org_id, topic, payload) VALUES ($1, 'session.ended', $2)",
                org_id,
                {
                    "session_id": str(session_id),
                    "reason": body.reason,
                    "is_valid": validity == "valid",
                },
            )

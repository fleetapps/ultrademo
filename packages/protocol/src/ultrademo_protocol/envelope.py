"""Realtime session protocol over LiveKit (docs/04 §1).

State that late joiners need lives in participant attributes; transcripts go over text streams;
discrete events go over reliable data packets wrapped in `Envelope`; request/response goes over RPC.

Transcripts and typed chat use LiveKit Agents' own text-stream topics rather than ours: the agent
framework already publishes every agent and viewer transcript segment on `lk.transcription` (with
`lk.segment_id` / `lk.transcription_final` attributes) and reads viewer chat from `lk.chat`
(livekit-agents 1.8 `voice/room_io`). Re-publishing them on a second topic would double the traffic.
The web player mirrors these constants in `apps/web/src/lib/protocol.ts`.
"""

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TOPIC_EVENTS = "ultrademo.events"
TOPIC_TRANSCRIPTION = "lk.transcription"
TOPIC_CHAT = "lk.chat"
# LiveKit Agents' own attributes on `lk.transcription` streams (livekit/agents/types.py).
ATTR_SEGMENT_ID = "lk.segment_id"
ATTR_TRANSCRIPTION_FINAL = "lk.transcription_final"
ATTR_AGENT_STATE = "ultrademo.agent_state"
# On the sandbox participant: JSON `{"viewport": {"w": int, "h": int}}`, the coordinate space of
# every overlay event and of the pointer RPC.
ATTR_SCREEN_META = "ultrademo.screen_meta"
ATTR_KIND = "ultrademo.kind"
RPC_CONFIRM = "ultrademo.confirm"
RPC_POINTER = "ultrademo.pointer"

# LiveKit RPC payloads are capped at 15 KiB; keep a margin for the JSON wrapper.
RPC_MAX_PAYLOAD_BYTES = 15 * 1024


class AgentState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTING = "acting"
    HANDOFF = "handoff"
    ENDED = "ended"


class EventType(StrEnum):
    ACTION_STARTED = "action.started"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
    OVERLAY_CURSOR = "overlay.cursor"
    OVERLAY_HIGHLIGHT = "overlay.highlight"
    OVERLAY_CLEAR = "overlay.clear"
    CTA_SHOW = "cta.show"
    CTA_HIDE = "cta.hide"
    SESSION_STATUS = "session.status"
    SIGNAL_RAISED = "signal.raised"
    COST_TICK = "cost.tick"
    CHAT_MESSAGE = "chat.message"
    CTA_ACTION = "cta.action"
    CONTROL = "control"
    CONTEXT_UPDATE = "context.update"


class Envelope(BaseModel):
    """Every reliable data packet on `TOPIC_EVENTS` is one of these, JSON-encoded."""

    model_config = ConfigDict(extra="allow")

    v: int = 1
    type: EventType
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    seq: int
    session_id: str

    def payload(self) -> dict[str, Any]:
        return self.model_extra or {}

    def encode(self) -> bytes:
        return self.model_dump_json().encode()

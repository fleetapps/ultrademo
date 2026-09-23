"""LiveKit Agents worker: one job per demo session.

    python -m livekit.agents download-files                                    # once, at build
    python -m livekit.agents start services/session-agent/src/ultrademo_agent/worker.py

(`cli.run_app` from a script is the deprecated legacy CLI in livekit-agents 1.8; the module entry
above discovers the `server` global. For local development with reload, use `lk agent dev`.)

The job is dispatched explicitly by agent name from the viewer's token (see the api's
livekit_tokens.py). It fetches the session context, starts a sandbox that streams the product into
the room, and runs a cascaded voice pipeline (Silero VAD + LiveKit turn detector, Deepgram STT,
ElevenLabs TTS) whose LLM step is replaced by our Claude brain via the documented `llm_node`
override (https://docs.livekit.io/agents/build/nodes/).
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    ModelSettings,
    TurnHandlingOptions,
    inference,
    llm,
    room_io,
)
from livekit.agents.types import FlushSentinel
from livekit.plugins import deepgram, elevenlabs, silero
from ultrademo_protocol import (
    ATTR_AGENT_STATE,
    RPC_CONFIRM,
    RPC_POINTER,
    TOPIC_EVENTS,
    AgentState,
    Envelope,
    ToolResult,
)
from ultrademo_protocol.envelope import EventType

from ultrademo_agent.brain import FLUSH, Brain, BrainConfig
from ultrademo_agent.clients import ApiClient, OperatorClient
from ultrademo_agent.pointer import Pointer
from ultrademo_agent.prompt import session_system, static_system
from ultrademo_agent.settings import Settings, get_settings

log = structlog.get_logger()

_background: set[asyncio.Task] = set()


def _spawn(coro: Any) -> None:
    """Fire-and-forget that keeps a reference, so the task is not garbage-collected mid-flight."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


class _BrainOwnsGeneration(llm.LLM):
    """Placeholder so AgentSession runs the pipeline; `DemoAgent.llm_node` never calls it."""

    @property
    def model(self) -> str:
        return "ultrademo-brain"

    def chat(self, **kwargs: Any) -> llm.LLMStream:
        raise RuntimeError("generation is handled by DemoAgent.llm_node")


class RoomHooks:
    """Brain side effects: realtime events to the viewer, persistence to the api."""

    def __init__(self, room: rtc.Room, api: ApiClient, session_id: str, settings: Settings) -> None:
        self._room = room
        self._api = api
        self._session_id = session_id
        self._settings = settings
        self._seq = 0
        self._action_seq = 0
        self.end_summary: str | None = None
        self.end_requested = asyncio.Event()
        self.handoff_requested = False

    def _viewer(self) -> str | None:
        identity = f"viewer_{self._session_id}"
        return identity if identity in self._room.remote_participants else None

    async def publish(self, type_: EventType, **payload: Any) -> None:
        self._seq += 1
        env = Envelope(type=type_, seq=self._seq, session_id=self._session_id, **payload)
        await self._room.local_participant.publish_data(
            env.encode(), reliable=True, topic=TOPIC_EVENTS
        )

    async def state(self, state: AgentState) -> None:
        await self._room.local_participant.set_attributes({ATTR_AGENT_STATE: state.value})

    async def action_started(self, tool: str, args: dict[str, Any]) -> None:
        await self.state(AgentState.ACTING)
        await self.publish(EventType.ACTION_STARTED, tool=tool, args=args)

    async def action_finished(self, tool: str, args: dict[str, Any], result: ToolResult) -> None:
        event = EventType.ACTION_FAILED if result.status == "error" else EventType.ACTION_COMPLETED
        await self.publish(
            event, tool=tool, status=result.status, label=result.summary, element=result.element
        )
        seq, self._action_seq = self._action_seq, self._action_seq + 1
        try:
            await self._api.actions(
                [
                    {
                        "seq": seq,
                        "tool": tool,
                        "args": args,
                        "status": result.status,
                        "result_summary": result.summary[:2000],
                        "policy_class": result.policy.value,
                        "element": result.element,
                        "latency_ms": result.latency_ms,
                    }
                ]
            )
        except Exception as e:  # noqa: BLE001 - never fail the call over bookkeeping
            log.warning("persist_action_failed", error=str(e)[:200])
        await self.state(AgentState.THINKING)

    async def confirm(self, summary: str, element: dict[str, Any] | None) -> bool:
        viewer = self._viewer()
        if viewer is None:
            return False
        try:
            answer = await self._room.local_participant.perform_rpc(
                destination_identity=viewer,
                method=RPC_CONFIRM,
                payload=json.dumps({"summary": summary, "element": element}),
                response_timeout=self._settings.confirm_timeout_s,
            )
            return json.loads(answer).get("approved") is True
        except Exception as e:  # noqa: BLE001 - timeout or an old player: treat as declined
            log.info("confirm_not_approved", error=str(e)[:200])
            return False

    async def show_cta(self, kind: str, label: str) -> str:
        await self.publish(EventType.CTA_SHOW, kind=kind, label=label)
        return f'The "{label}" button is now on the viewer\'s screen.'

    async def handoff(self, reason: str) -> str:
        self.handoff_requested = True
        await self.publish(EventType.SESSION_STATUS, status="handoff_requested", reason=reason)
        return "The sales team has been notified. Tell the viewer someone will follow up, and keep helping."

    async def end(self, summary: str) -> None:
        self.end_summary = summary
        self.end_requested.set()


class DemoAgent(Agent):
    def __init__(self, brain: Brain) -> None:
        super().__init__(instructions="(handled by the ultrademo brain)")
        self.brain = brain

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list, model_settings: ModelSettings):
        last = next(
            (m for m in reversed(chat_ctx.messages()) if m.role in ("user", "assistant")), None
        )
        user_text = last.text_content if last is not None and last.role == "user" else None
        async for chunk in self.brain.respond(user_text):
            yield FlushSentinel() if chunk is FLUSH else chunk


class Transcript:
    """Buffers committed lines and ships them to the api in small batches."""

    def __init__(self, api: ApiClient) -> None:
        self._api = api
        self._seq = 0
        self._buf: list[dict[str, Any]] = []
        self.participant_turns = 0

    def add(self, item: Any) -> None:
        if getattr(item, "type", None) != "message" or item.role not in ("user", "assistant"):
            return
        text = item.text_content or ""
        if not text.strip():
            return
        if item.role == "user":
            self.participant_turns += 1
        self._buf.append(
            {
                "seq": self._seq,
                "role": "participant" if item.role == "user" else "agent",
                "content": text,
                "interrupted": bool(getattr(item, "interrupted", False)),
                "started_at": datetime.fromtimestamp(item.created_at, UTC).isoformat(),
            }
        )
        self._seq += 1

    async def flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            await self._api.transcript(batch)
        except Exception as e:  # noqa: BLE001
            self._buf = batch + self._buf
            log.warning("persist_transcript_failed", error=str(e)[:200])


server = AgentServer()


def _brain_config(settings: Settings, agent_model: dict[str, Any]) -> BrainConfig:
    return BrainConfig(
        model=agent_model.get("id", settings.model),
        effort=agent_model.get("effort", settings.effort),
        max_tokens=settings.max_tokens,
        max_tool_rounds=settings.max_tool_rounds,
        fallbacks=settings.fallbacks,
        clear_tool_results_at_tokens=settings.clear_tool_results_at_tokens,
    )


@server.rtc_session(agent_name=get_settings().agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    session_id = json.loads(ctx.job.metadata or "{}")["session_id"]
    token = settings.internal_token.get_secret_value()
    api = ApiClient(settings.api_url, token, session_id)
    operator = OperatorClient(settings.operator_url, token)
    sc = await api.context()
    log.info("session_job_started", session_id=session_id, room=ctx.room.name)

    hooks = RoomHooks(ctx.room, api, session_id, settings)
    brain = Brain(
        anthropic.AsyncAnthropic(),
        operator,
        hooks,
        static_system=static_system(sc["agent_version"]["system_prompt"], sc["product"]),
        session_system=session_system(sc),
        config=_brain_config(settings, sc["agent_version"].get("model") or {}),
    )
    transcript = Transcript(api)
    end_reason = "viewer_left"

    async def finalize(reason: str) -> None:
        await transcript.flush()
        rows = brain.take_usage().ledger(brain.served_model)
        results = await asyncio.gather(
            api.cost(rows) if rows else asyncio.sleep(0),
            api.ended(end_reason, hooks.end_summary, transcript.participant_turns),
            operator.stop(),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.warning("finalize_step_failed", error=str(r)[:200])
        await asyncio.gather(api.aclose(), operator.aclose(), return_exceptions=True)
        log.info("session_job_ended", session_id=session_id, reason=reason, usage=vars(brain.usage))

    ctx.add_shutdown_callback(finalize)

    # Start the screen before the voice, so the viewer never hears "look at this" over a black tile.
    await operator.start(
        session_id=session_id,
        start_url=sc["product"]["base_url"],
        allowed_domains=sc["product"]["allowed_domains"],
        policy=sc["agent_version"].get("policy") or {},
        livekit_room=ctx.room.name,
        # The player draws the cursor and highlights from the operator's overlay events (ADR 4).
        draw_overlays=False,
    )

    voice = sc["agent_version"].get("voice") or {}
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model=settings.stt_model, language=voice.get("stt_language", "multi")),
        tts=elevenlabs.TTS(
            model=voice.get("model", settings.tts_model),
            voice_id=voice.get("voice_id", settings.tts_voice_id),
        ),
        llm=_BrainOwnsGeneration(),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            # Preemptive generation would start a second (billed) Claude request per turn.
            preemptive_generation={"enabled": False},
        ),
    )

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        transcript.add(ev.item)
        _spawn(transcript.flush())

    closed = asyncio.Event()

    @session.on("close")
    def _on_close(ev: Any) -> None:
        closed.set()

    @session.on("agent_state_changed")
    def _on_state(ev: Any) -> None:
        mapping = {
            "listening": AgentState.LISTENING,
            "thinking": AgentState.THINKING,
            "speaking": AgentState.SPEAKING,
            "idle": AgentState.IDLE,
        }
        if ev.new_state in mapping:
            _spawn(hooks.state(mapping[ev.new_state]))

    async def cost_ticker() -> None:
        while True:
            await asyncio.sleep(30)
            rows = brain.take_usage().ledger(brain.served_model)
            if rows:
                try:
                    await api.cost(rows)
                except Exception as e:  # noqa: BLE001
                    log.warning("persist_cost_failed", error=str(e)[:200])

    viewer = f"viewer_{session_id}"
    await session.start(
        room=ctx.room,
        agent=DemoAgent(brain),
        room_options=room_io.RoomOptions(
            # Listen to the viewer only, never to the sandbox participant or a late joiner.
            participant_identity=viewer,
            # Deleting the room ends the sandbox's stream and the viewer's connection at once.
            delete_room_on_close=True,
        ),
    )

    async def ask(text: str) -> None:
        try:
            await session.interrupt()
        except RuntimeError:
            pass  # the current speech cannot be interrupted; the question queues behind it
        session.generate_reply(user_input=text)

    pointer = Pointer(operator, brain, ask, viewer_identity=viewer)

    @ctx.room.local_participant.register_rpc_method(RPC_POINTER)
    async def _on_pointer(data: rtc.RpcInvocationData) -> str:
        return await pointer.handle(data.caller_identity, data.payload)

    await api.started()
    session.generate_reply()  # llm_node sees no new viewer message and greets

    ticker = asyncio.create_task(cost_ticker())
    try:
        ended = asyncio.create_task(hooks.end_requested.wait())
        left = asyncio.create_task(closed.wait())
        done, pending = await asyncio.wait(
            [ended, left], timeout=sc["max_duration_s"], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if ended in done:
            end_reason = "agent_ended"
            await asyncio.sleep(1.5)  # let the goodbye finish playing
        elif left in done:
            end_reason = "viewer_left"
        else:
            end_reason = "max_duration"
            handle = session.say(
                "We're at the end of our time. Thanks for joining, the team will follow up."
            )
            await handle.wait_for_playout()
    finally:
        ticker.cancel()
        await hooks.publish(EventType.SESSION_STATUS, status="ended", reason=end_reason)
        await hooks.state(AgentState.ENDED)
        ctx.shutdown(reason=end_reason)

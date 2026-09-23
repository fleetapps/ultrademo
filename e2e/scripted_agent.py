"""A scripted stand-in for the session agent, for the browser end-to-end test.

It runs on the real LiveKit Agents worker (dispatched by name from the viewer's token), and uses
the real `RoomHooks`, `Pointer`, api and operator clients. Only the voice pipeline and Claude are
replaced: the viewer types commands, and the script answers on the same text streams LiveKit
Agents uses (`lk.chat` in, `lk.transcription` out), so the player sees exactly the wire it gets in
production, without any API keys.

    python -m livekit.agents start e2e/scripted_agent.py --url ws://localhost:7880 \
        --api-key devkey --api-secret secret

Commands the test types: "open hooli", "save it", "pricing", "bye".
"""

import asyncio
import json
import re
import uuid
from typing import Any

from livekit import rtc
from livekit.agents import AgentServer, JobContext
from ultrademo_agent.clients import ApiClient, OperatorClient
from ultrademo_agent.pointer import Pointer
from ultrademo_agent.settings import get_settings
from ultrademo_agent.worker import RoomHooks
from ultrademo_protocol import (
    ATTR_SEGMENT_ID,
    ATTR_TRANSCRIPTION_FINAL,
    RPC_POINTER,
    TOPIC_CHAT,
    TOPIC_TRANSCRIPTION,
    AgentState,
    EventType,
)

server = AgentServer(
    # LiveKit Agents sends all of its traffic through HTTPS_PROXY when it is set, ignoring
    # NO_PROXY. The test's LiveKit server is local, so the worker connects to it directly.
    http_proxy=None,
    # The default load is host CPU, which a busy CI runner can push past the threshold, and a
    # "full" worker is never dispatched. This worker runs one scripted job at a time.
    load_fnc=lambda: 0.0,
)


class _Notes:
    """Stands in for the brain's `note()`; the scripted reply reads the latest note."""

    def __init__(self) -> None:
        self.last = ""

    def note(self, text: str) -> None:
        self.last = text


def _ref(snapshot: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*\[ref=(\w+)\]', snapshot)
    if not m:
        raise LookupError(f"{role} {name!r} not in snapshot:\n{snapshot[:2000]}")
    return m.group(1)


@server.rtc_session(agent_name=get_settings().agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    session_id = json.loads(ctx.job.metadata or "{}")["session_id"]
    token = settings.internal_token.get_secret_value()
    api = ApiClient(settings.api_url, token, session_id)
    operator = OperatorClient(settings.operator_url, token)
    sc = await api.context()
    await ctx.connect()
    room = ctx.room
    hooks = RoomHooks(room, api, session_id, settings)
    viewer = f"viewer_{session_id}"
    lp = room.local_participant

    async def say(text: str) -> None:
        """Agent speech as LiveKit Agents publishes it: one delta stream per segment."""
        await hooks.state(AgentState.SPEAKING)
        seg = f"SG_{uuid.uuid4().hex[:12]}"
        writer = await lp.stream_text(
            topic=TOPIC_TRANSCRIPTION,
            attributes={ATTR_TRANSCRIPTION_FINAL: "false", ATTR_SEGMENT_ID: seg},
        )
        for word in text.split(" "):
            await writer.write(word + " ")
            await asyncio.sleep(0.03)
        await writer.aclose(attributes={ATTR_TRANSCRIPTION_FINAL: "true"})
        await hooks.state(AgentState.LISTENING)

    async def heard(text: str) -> None:
        """Viewer speech as LiveKit Agents publishes STT: a new stream per update, sent on the
        viewer's behalf (`sender_identity`), the last one marked final."""
        seg = f"SG_{uuid.uuid4().hex[:12]}"
        words = text.split(" ")
        for i in range(1, len(words) + 1):
            final = "true" if i == len(words) else "false"
            writer = await lp.stream_text(
                topic=TOPIC_TRANSCRIPTION,
                sender_identity=viewer,
                attributes={ATTR_TRANSCRIPTION_FINAL: final, ATTR_SEGMENT_ID: seg},
            )
            await writer.write(" ".join(words[:i]))
            await writer.aclose()
            await asyncio.sleep(0.05)

    async def act(tool: str, args: dict[str, Any]) -> Any:
        """The brain's tool path (brain.py `_run_tool`), minus the model."""
        await hooks.action_started(tool, args)
        res = await operator.run(tool, args)
        if res.status == "needs_confirmation":
            if await hooks.confirm(res.summary, res.element):
                res = await operator.run(tool, args, confirmed=True)
            else:
                res = res.model_copy(update={"status": "blocked", "summary": "Declined."})
        await hooks.action_finished(tool, args, res)
        return res

    refs: dict[str, str] = {}

    async def command(text: str) -> None:
        cmd = text.strip().lower()
        if cmd == "open hooli":
            snap = (await act("operate_observe", {})).snapshot or ""
            await act("operate_click", {"ref": _ref(snap, "link", "Hooli renewal")})
            snap = (await act("operate_observe", {})).snapshot or ""
            refs["save"] = _ref(snap, "button", "Save deal")
            await act("operate_highlight", {"ref": refs["save"], "label": "Save the deal here"})
            await say("This is the Hooli renewal. You save changes with this button.")
        elif cmd == "save it":
            res = await act("operate_click", {"ref": refs["save"]})
            await say("Saved." if res.status == "ok" else "Okay, I left it as it was.")
        elif cmd == "pricing":
            await hooks.show_cta("book", "Book a call")
            await say("Pricing depends on seats. The sales team can walk you through it.")
        elif cmd == "bye":
            await say("Thanks for your time. Goodbye.")
            await hooks.end("The viewer looked at the Hooli renewal.")
        else:
            await say(f"I heard: {text}")

    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_chat(reader: rtc.TextStreamReader, identity: str) -> None:
        async def read() -> None:
            text = await reader.read_all()
            if identity == viewer:
                queue.put_nowait(text)

        asyncio.ensure_future(read())

    room.register_text_stream_handler(TOPIC_CHAT, on_chat)

    notes = _Notes()

    async def ask(question: str) -> None:
        await say(f"{notes.last.strip('[]')} {question}")

    pointer = Pointer(operator, notes, ask, viewer_identity=viewer)

    @lp.register_rpc_method(RPC_POINTER)
    async def _on_pointer(data: rtc.RpcInvocationData) -> str:
        return await pointer.handle(data.caller_identity, data.payload)

    left = asyncio.Event()

    @room.on("participant_disconnected")
    def _on_left(p: rtc.RemoteParticipant) -> None:
        if p.identity == viewer:
            left.set()

    await operator.start(
        session_id=session_id,
        start_url=sc["product"]["base_url"],
        allowed_domains=sc["product"]["allowed_domains"],
        policy=sc["agent_version"].get("policy") or {},
        livekit_room=room.name,
        draw_overlays=False,
    )
    await ctx.wait_for_participant(identity=viewer)
    await api.started()
    await say("Hi, I'm Ava. What would you like to see?")
    await heard("Show me a deal")

    reason = "viewer_left"
    try:
        while not hooks.end_requested.is_set() and not left.is_set():
            get = asyncio.ensure_future(queue.get())
            gone = asyncio.ensure_future(left.wait())
            done, _ = await asyncio.wait([get, gone], return_when=asyncio.FIRST_COMPLETED)
            gone.cancel()
            if get in done:
                await command(get.result())
            else:
                get.cancel()
        if hooks.end_requested.is_set():
            reason = "agent_ended"
    finally:
        await hooks.publish(EventType.SESSION_STATUS, status="ended", reason=reason)
        await hooks.state(AgentState.ENDED)
        await asyncio.gather(
            api.ended(reason, hooks.end_summary, 4),
            operator.stop(),
            return_exceptions=True,
        )
        await asyncio.gather(api.aclose(), operator.aclose(), return_exceptions=True)
        await asyncio.sleep(0.5)  # let the ended event reach the viewer before the room goes
        await ctx.delete_room()
        ctx.shutdown(reason=reason)

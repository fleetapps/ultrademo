"""The session brain: one Claude conversation that talks and operates the product.

Deliberately independent of LiveKit, so it can be unit-tested and reused by the eval simulator.
`respond()` streams speakable text; the voice layer pipes it into TTS as it arrives. Between the
model's spoken preamble and its tool calls it yields `FLUSH`, so the viewer hears "let me open
approvals" while the click happens, instead of waiting for the whole turn.

Request shape (checked against the Claude API docs, see docs/04 §2 notes):
- Opus 5 with adaptive thinking (its default when `thinking` is omitted) and a per-agent effort.
- Frozen, strict tool list; explicit cache breakpoint on the static system block plus top-level
  automatic caching for the growing conversation.
- Context editing clears old tool results once the prompt is large (snapshots dominate tokens).
- `fallbacks: "default"` re-runs a declined turn on the recommended fallback model server-side.
- Stop reasons are checked before any tool runs; refused or truncated turns never execute tools.
- Interruptions (the viewer talks over the agent) cancel the task; history is repaired so every
  tool_use still has a tool_result and the next request is valid.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import structlog
from ultrademo_protocol import ToolResult, claude_tool_definitions, parse_tool_input
from ultrademo_protocol.tools import OPERATOR_TOOLS, ToolInputError

from ultrademo_agent.pricing import Usage
from ultrademo_agent.prompt import KICKOFF

log = structlog.get_logger()


class _Flush:
    def __repr__(self) -> str:
        return "FLUSH"


FLUSH = _Flush()

BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_CONTEXT_EDITING = "context-management-2025-06-27"

TOOLS = claude_tool_definitions()

INTERRUPTED = (
    "Interrupted: the viewer started speaking before this finished. It may or may not have taken "
    "effect; observe the screen before relying on it."
)


class Operator(Protocol):
    async def run(
        self, tool: str, tool_input: dict[str, Any], confirmed: bool = False
    ) -> ToolResult: ...


class Hooks(Protocol):
    async def action_started(self, tool: str, args: dict[str, Any]) -> None: ...
    async def action_finished(
        self, tool: str, args: dict[str, Any], result: ToolResult
    ) -> None: ...
    async def confirm(self, summary: str, element: dict[str, Any] | None) -> bool: ...
    async def show_cta(self, kind: str, label: str) -> str: ...
    async def handoff(self, reason: str) -> str: ...
    async def end(self, summary: str) -> None: ...


@dataclass
class BrainConfig:
    model: str = "claude-opus-5"
    effort: str = "low"
    max_tokens: int = 4096
    max_tool_rounds: int = 12
    fallbacks: bool = True
    clear_tool_results_at_tokens: int = 60_000


@dataclass
class _ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    result: dict[str, Any] | None = field(default=None)


def render_result(result: ToolResult) -> list[dict[str, Any]] | str:
    lines = [f"status: {result.status}", f"summary: {result.summary}"]
    if result.url:
        lines.append(f"url: {result.url}")
    if result.title:
        lines.append(f"title: {result.title}")
    if result.snapshot:
        lines.append("screen:\n" + result.snapshot)
    text = "\n".join(lines)
    if not result.screenshot_jpeg_b64:
        return text
    return [
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": result.screenshot_jpeg_b64,
            },
        },
    ]


class Brain:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        operator: Operator,
        hooks: Hooks,
        *,
        static_system: str,
        session_system: str,
        config: BrainConfig | None = None,
    ) -> None:
        self._client = client
        self._operator = operator
        self._hooks = hooks
        self.config = config or BrainConfig()
        self._system = [
            {"type": "text", "text": static_system, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": session_system},
        ]
        self.messages: list[dict[str, Any]] = []
        self.usage = Usage()
        self._unreported = Usage()
        self.served_model = self.config.model
        self.ended = False

    # --- request -------------------------------------------------------------------------------

    def request_params(self) -> dict[str, Any]:
        c = self.config
        betas = [BETA_CONTEXT_EDITING]
        params: dict[str, Any] = {
            "model": c.model,
            "max_tokens": c.max_tokens,
            "system": self._system,
            "tools": TOOLS,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": c.effort},
            "context_management": {
                "edits": [
                    {
                        "type": "clear_tool_uses_20250919",
                        "trigger": {
                            "type": "input_tokens",
                            "value": c.clear_tool_results_at_tokens,
                        },
                        "keep": {"type": "tool_uses", "value": 4},
                        # Only break the cache when clearing saves a meaningful amount.
                        "clear_at_least": {"type": "input_tokens", "value": 10_000},
                    }
                ]
            },
        }
        if c.fallbacks:
            params["fallbacks"] = "default"
            betas.append(BETA_FALLBACK)
        params["betas"] = betas
        return params

    def take_usage(self) -> Usage:
        u, self._unreported = self._unreported, Usage()
        return u

    # --- turn ----------------------------------------------------------------------------------

    async def respond(self, user_text: str | None) -> AsyncIterator[str | _Flush]:
        """Handle one viewer turn (or the join, when `user_text` is None)."""
        self.messages.append({"role": "user", "content": user_text or KICKOFF})
        for _ in range(self.config.max_tool_rounds):
            spoken: list[str] = []
            try:
                async with self._client.beta.messages.stream(**self.request_params()) as stream:
                    async for event in stream:
                        if event.type == "text" and event.text:
                            spoken.append(event.text)
                            yield event.text
                    final = await stream.get_final_message()
            except (asyncio.CancelledError, GeneratorExit):
                # The viewer interrupted mid-sentence: keep what was said, drop the rest.
                if spoken:
                    self.messages.append({"role": "assistant", "content": "".join(spoken)})
                raise
            except anthropic.APIError as e:
                log.warning("claude_request_failed", error=type(e).__name__, detail=str(e)[:300])
                if spoken:
                    self.messages.append({"role": "assistant", "content": "".join(spoken)})
                yield " Sorry, I lost my connection for a moment. Could you say that again?"
                return

            self._account(final)
            stop = final.stop_reason
            tool_uses = [b for b in final.content if b.type == "tool_use"]

            if stop == "refusal":
                log.info("claude_refusal", category=getattr(final.stop_details, "category", None))
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": "I can't help with that one, but I'm happy to keep going.",
                    }
                )
                yield " I can't help with that one, but I'm happy to keep going."
                return
            if stop == "max_tokens" and tool_uses:
                # A truncated tool input can parse as a valid partial object; never run it.
                text = "".join(spoken) or "Let me try that differently."
                self.messages.append({"role": "assistant", "content": text})
                return

            self.messages.append({"role": "assistant", "content": final.content})
            if stop == "pause_turn":
                continue
            if stop != "tool_use" or not tool_uses:
                return

            calls = [_ToolCall(b.id, b.name, b.input) for b in tool_uses]
            try:
                yield FLUSH  # speak the preamble while the tools run
                for call in calls:
                    call.result = await self._run_tool(call)
            except (asyncio.CancelledError, GeneratorExit):
                # Every tool_use needs a tool_result or the next request is rejected.
                self._append_results(calls)
                raise
            self._append_results(calls)
            if self.ended:
                return
        log.warning("tool_round_limit", rounds=self.config.max_tool_rounds)

    def _append_results(self, calls: list[_ToolCall]) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    c.result
                    or {
                        "type": "tool_result",
                        "tool_use_id": c.id,
                        "is_error": True,
                        "content": INTERRUPTED,
                    }
                    for c in calls
                ],
            }
        )

    def _account(self, final: Any) -> None:
        u = final.usage
        turn = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        )
        self.usage.add(turn)
        self._unreported.add(turn)
        self.served_model = final.model or self.config.model

    # --- tools ---------------------------------------------------------------------------------

    async def _run_tool(self, call: _ToolCall) -> dict[str, Any]:
        def result(content: Any, is_error: bool = False) -> dict[str, Any]:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": content,
            }
            if is_error:
                block["is_error"] = True
            return block

        try:
            args = parse_tool_input(call.name, call.input)
        except ToolInputError as e:
            return result(f"INVALID_INPUT: {e}", is_error=True)
        raw = args.model_dump(exclude_none=True)

        if call.name in OPERATOR_TOOLS:
            await self._hooks.action_started(call.name, raw)
            try:
                res = await self._operator.run(call.name, raw)
                if res.status == "needs_confirmation":
                    approved = await self._hooks.confirm(res.summary, res.element)
                    if approved:
                        res = await self._operator.run(call.name, raw, confirmed=True)
                    else:
                        res = ToolResult(
                            status="blocked",
                            summary=f"The viewer declined: {res.summary}. Nothing changed.",
                            policy=res.policy,
                            element=res.element,
                        )
            except Exception as e:  # noqa: BLE001 - operator/network faults become tool errors
                log.warning("operator_call_failed", tool=call.name, error=str(e)[:300])
                res = ToolResult(
                    status="error",
                    summary="The screen did not respond. Try again or continue talking.",
                )
            await self._hooks.action_finished(call.name, raw, res)
            return result(render_result(res), is_error=res.status == "error")

        if call.name == "ui_show_cta":
            return result(await self._hooks.show_cta(raw["kind"], raw["label"]))
        if call.name == "session_handoff":
            return result(await self._hooks.handoff(raw["reason"]))
        if call.name == "session_end":
            self.ended = True
            await self._hooks.end(raw["summary"])
            return result("Session is ending. Do not say anything else.")
        return result(f"Unknown tool {call.name}", is_error=True)


def dumps_messages(messages: list[dict[str, Any]]) -> str:
    """Debug helper: history as JSON (SDK blocks included)."""

    def default(o: Any) -> Any:
        return o.model_dump() if hasattr(o, "model_dump") else str(o)

    return json.dumps(messages, default=default, indent=2)

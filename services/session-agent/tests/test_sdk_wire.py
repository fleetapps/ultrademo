"""Drive the Brain through the real Anthropic SDK against a mocked HTTP transport.

This checks what the fakes in test_brain.py cannot: that SDK 1.8 accepts our request parameters,
sends the beta headers we expect, and that our stream handling matches the SDK's real SSE parser
(text events, tool_use accumulation, stop reasons, usage).
"""

import json

import anthropic
import httpx2
from ultrademo_agent.brain import FLUSH, Brain
from ultrademo_agent.prompt import session_system, static_system
from ultrademo_protocol import ToolResult


def sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def turn(blocks: list[dict], stop: str) -> bytes:
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 4000,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        )
    ]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": b["text"]},
                    },
                )
            )
        else:
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": b["id"],
                            "name": b["name"],
                            "input": {},
                        },
                    },
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(b["input"]),
                        },
                    },
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 30},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return sse(events)


class Op:
    def __init__(self):
        self.calls = []

    async def run(self, tool, tool_input, confirmed=False):
        self.calls.append((tool, tool_input))
        return ToolResult(status="ok", summary="Clicked", snapshot='- heading "Approvals" [ref=e3]')


class Hooks:
    async def action_started(self, *a): ...
    async def action_finished(self, *a): ...
    async def confirm(self, *a):
        return True

    async def show_cta(self, *a):
        return "ok"

    async def handoff(self, *a):
        return "ok"

    async def end(self, *a): ...


async def test_brain_over_real_sdk_stream():
    bodies = [
        turn(
            [
                {"type": "text", "text": "Let me open approvals."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "operate_click",
                    "input": {"ref": "e4"},
                },
            ],
            "tool_use",
        ),
        turn([{"type": "text", "text": "Here it is."}], "end_turn"),
    ]
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=bodies[len(requests) - 1]
        )

    client = anthropic.AsyncAnthropic(
        api_key="test",
        base_url="https://api.test",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    op = Op()
    brain = Brain(
        client,
        op,
        Hooks(),
        static_system=static_system("Show approvals.", {"name": "Acme", "base_url": "https://a"}),
        session_system=session_system({"locale": "en"}),
    )
    out = [x async for x in brain.respond("show approvals")]

    assert "".join(x for x in out if isinstance(x, str)) == "Let me open approvals.Here it is."
    assert FLUSH in out and op.calls == [("operate_click", {"ref": "e4"})]

    first = requests[0]
    assert first.url.path == "/v1/messages"
    assert set(first.headers["anthropic-beta"].split(",")) == {
        "context-management-2025-06-27",
        "server-side-fallback-2026-07-01",
    }
    body = json.loads(first.content)
    assert body["stream"] is True and body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"} and body["output_config"] == {
        "effort": "low"
    }
    assert [t["name"] for t in body["tools"]][:2] == ["operate_observe", "operate_navigate"]
    assert all(t["strict"] is True for t in body["tools"])

    second = json.loads(requests[1].content)
    # The SDK's own content blocks are echoed back, then our tool_result.
    assert second["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "operate_click",
        "input": {"ref": "e4"},
    }
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert brain.usage.cache_read_input_tokens == 8000 and brain.usage.output_tokens == 60

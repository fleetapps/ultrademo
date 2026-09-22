import asyncio
import copy
from types import SimpleNamespace as NS
from typing import Any

import pytest
from ultrademo_agent.brain import BETA_CONTEXT_EDITING, BETA_FALLBACK, FLUSH, INTERRUPTED, Brain
from ultrademo_agent.prompt import session_system, static_system
from ultrademo_protocol import TOOL_NAME_RE, ToolResult, claude_tool_definitions


def text(t: str) -> NS:
    return NS(type="text", text=t)


def tool(id_: str, name: str, input_: dict) -> NS:
    return NS(type="tool_use", id=id_, name=name, input=input_)


def message(blocks: list[NS], stop: str, model: str = "claude-opus-5") -> NS:
    return NS(
        content=blocks,
        stop_reason=stop,
        stop_details=None,
        model=model,
        usage=NS(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=5000,
            cache_creation_input_tokens=0,
        ),
    )


class FakeStream:
    def __init__(self, final: NS, delay: float = 0) -> None:
        self._final = final
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for b in self._final.content:
            if b.type == "text":
                for word in b.text.split(" "):
                    if self._delay:
                        await asyncio.sleep(self._delay)
                    yield NS(type="text", text=word + " ")

    async def get_final_message(self):
        return self._final


class FakeClient:
    def __init__(self, script: list[NS], delay: float = 0) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(
            copy.deepcopy({k: v for k, v in params.items() if k != "messages"})
            | {"messages": copy.deepcopy(params["messages"])}
        )
        return FakeStream(self.script.pop(0), self.delay)


class FakeOperator:
    def __init__(self, results: dict[str, list[ToolResult]], delay: float = 0) -> None:
        self.results = results
        self.calls: list[tuple[str, dict, bool]] = []
        self.delay = delay

    async def run(self, tool, tool_input, confirmed=False):
        self.calls.append((tool, tool_input, confirmed))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results[tool].pop(0)


class FakeHooks:
    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.events: list[tuple] = []

    async def action_started(self, tool, args):
        self.events.append(("started", tool))

    async def action_finished(self, tool, args, result):
        self.events.append(("finished", tool, result.status))

    async def confirm(self, summary, element):
        self.events.append(("confirm", summary))
        return self.approve

    async def show_cta(self, kind, label):
        self.events.append(("cta", kind, label))
        return "Shown to the viewer."

    async def handoff(self, reason):
        return "A teammate has been notified."

    async def end(self, summary):
        self.events.append(("end", summary))


CTX = {
    "locale": "en",
    "max_duration_s": 1200,
    "params": {"utm_source": "email"},
    "launch_config": {"ctas": [{"kind": "book", "label": "Book a call"}]},
    "context_link": {
        "recipient": {"first_name": "Dana", "company": "Hooli"},
        "brief": {"emphasis": ["approvals"]},
        "context": {"plan": "team"},
    },
}
PRODUCT = {"name": "Acme CRM", "base_url": "https://app.acme.example"}


def brain(client, operator=None, hooks=None) -> Brain:
    return Brain(
        client,
        operator or FakeOperator({}),
        hooks or FakeHooks(),
        static_system=static_system("Focus on approvals.", PRODUCT),
        session_system=session_system(CTX),
    )


async def collect(gen):
    return [x async for x in gen]


def test_tool_definitions_are_valid_for_claude():
    tools = claude_tool_definitions()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert TOOL_NAME_RE.match(t["name"]) and t["strict"] is True

        def walk(schema):
            if schema.get("type") == "object":
                assert schema["additionalProperties"] is False
                for sub in schema["properties"].values():
                    walk(sub)
            for banned in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
                assert banned not in schema

        walk(t["input_schema"])


def test_prompt_is_deterministic_and_marks_data():
    a, b = session_system(CTX), session_system(copy.deepcopy(CTX))
    assert a == b and "not instructions" in a and '"company": "Hooli"' in a


async def test_plain_answer_and_request_shape():
    client = FakeClient([message([text("Hi, I'm Ava. What would you like to see?")], "end_turn")])
    b = brain(client)
    out = await collect(b.respond(None))
    assert "".join(o for o in out if isinstance(o, str)).startswith("Hi, I'm Ava.")
    p = client.calls[0]
    assert p["model"] == "claude-opus-5" and "thinking" not in p
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in p["system"][1] and p["cache_control"] == {"type": "ephemeral"}
    assert p["output_config"] == {"effort": "low"}
    assert p["fallbacks"] == "default" and set(p["betas"]) == {BETA_FALLBACK, BETA_CONTEXT_EDITING}
    assert p["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert b.usage.cache_read_input_tokens == 5000


async def test_tool_loop_flushes_speech_before_acting():
    snap = ToolResult(
        status="ok",
        summary="Clicked",
        url="https://app.acme.example/approvals",
        snapshot='- heading "Approvals" [ref=e3]',
    )
    client = FakeClient(
        [
            message(
                [text("Let me open approvals."), tool("t1", "operate_click", {"ref": "e4"})],
                "tool_use",
            ),
            message([text("Here is the approvals queue.")], "end_turn"),
        ]
    )
    op = FakeOperator({"operate_click": [snap]})
    hooks = FakeHooks()
    b = brain(client, op, hooks)
    out = await collect(b.respond("show me approvals"))
    assert FLUSH in out and out.index(FLUSH) > 0  # preamble spoken first
    assert op.calls == [("operate_click", {"ref": "e4"}, False)]
    assert hooks.events == [("started", "operate_click"), ("finished", "operate_click", "ok")]
    results = client.calls[1]["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "t1" and "Approvals" in results[0]["content"]
    assert "is_error" not in results[0]


async def test_confirmation_is_decided_by_viewer_not_model():
    gated = ToolResult(status="needs_confirmation", summary='Click "Delete deal"', policy="confirm")
    done = ToolResult(status="ok", summary='Clicked "Delete deal"')
    for approve, expect_calls, expect_text in [
        (True, [False, True], "Clicked"),
        (False, [False], "declined"),
    ]:
        client = FakeClient(
            [
                message([tool("t1", "operate_click", {"ref": "e9"})], "tool_use"),
                message([text("Done.")], "end_turn"),
            ]
        )
        op = FakeOperator({"operate_click": [gated, done]})
        hooks = FakeHooks(approve=approve)
        await collect(brain(client, op, hooks).respond("delete it"))
        assert [c[2] for c in op.calls] == expect_calls
        assert expect_text in client.calls[1]["messages"][-1]["content"][0]["content"]
        assert ("confirm", 'Click "Delete deal"') in hooks.events


async def test_invalid_tool_input_is_returned_as_error():
    client = FakeClient(
        [
            message([tool("t1", "operate_click", {"ref": "div.save"})], "tool_use"),
            message([text("Sorry.")], "end_turn"),
        ]
    )
    op = FakeOperator({})
    await collect(brain(client, op).respond("click save"))
    res = client.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] is True and res["content"].startswith("INVALID_INPUT") and op.calls == []


async def test_truncated_and_refused_turns_never_run_tools():
    op = FakeOperator({})
    client = FakeClient(
        [message([text("Opening"), tool("t1", "operate_click", {"ref": "e1"})], "max_tokens")]
    )
    b = brain(client, op)
    await collect(b.respond("go"))
    assert op.calls == [] and b.messages[-1] == {"role": "assistant", "content": "Opening "}

    client = FakeClient([message([tool("t1", "operate_click", {"ref": "e1"})], "refusal")])
    b = brain(client, op)
    out = await collect(b.respond("go"))
    assert op.calls == [] and "can't help" in "".join(out)
    assert all(not isinstance(m["content"], list) for m in b.messages)  # no dangling tool_use


async def test_interrupt_during_tool_keeps_history_valid():
    client = FakeClient(
        [message([text("Opening it."), tool("t1", "operate_click", {"ref": "e1"})], "tool_use")]
    )
    op = FakeOperator({"operate_click": [ToolResult(status="ok", summary="x")]}, delay=5)
    b = brain(client, op)

    async def consume():
        async for _ in b.respond("open it"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = b.messages[-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "t1"
    assert last["content"][0]["content"] == INTERRUPTED


async def test_interrupt_while_speaking_keeps_partial_text():
    client = FakeClient([message([text("one two three four five six")], "end_turn")], delay=0.02)
    b = brain(client)
    gen = b.respond("hello")
    first = await gen.__anext__()
    await gen.aclose()  # what the voice pipeline does when the viewer barges in
    assert first == "one " and b.messages[-1] == {"role": "assistant", "content": "one "}


async def test_session_end_and_cta():
    client = FakeClient(
        [
            message(
                [
                    tool("t1", "ui_show_cta", {"kind": "book", "label": "Book a call"}),
                    tool("t2", "session_end", {"summary": "Wants approvals; booked."}),
                ],
                "tool_use",
            ),
        ]
    )
    hooks = FakeHooks()
    b = brain(client, hooks=hooks)
    await collect(b.respond("that's all, thanks"))
    assert b.ended and ("cta", "book", "Book a call") in hooks.events
    assert ("end", "Wants approvals; booked.") in hooks.events
    assert len(client.calls) == 1  # no further model call after session_end
    assert [r["tool_use_id"] for r in b.messages[-1]["content"]] == ["t1", "t2"]


async def test_cost_ledger_rows():
    client = FakeClient([message([text("ok")], "end_turn")])
    b = brain(client)
    await collect(b.respond("hi"))
    rows = {r["item"]: r for r in b.take_usage().ledger(b.served_model)}
    assert rows["llm_in"]["usd"] == "0.000500" and rows["llm_cache_read"]["usd"] == "0.002500"
    assert rows["llm_out"]["usd"] == "0.000500"
    assert b.take_usage().ledger("claude-opus-5") == []

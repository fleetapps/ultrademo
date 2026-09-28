"""The agent's HTTP clients call the exact paths the api and operator serve."""

import json

import httpx
from ultrademo_agent.clients import ApiClient, OperatorClient


def _recorder() -> tuple[list[tuple[str, str]], httpx.MockTransport]:
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer t"
        path = request.url.path
        if path == "/v1/sandboxes":
            return httpx.Response(
                200, json={"sandbox_id": "sb1", "url": "u", "title": "t", "streaming": True}
            )
        if path.endswith("/element-at"):
            return httpx.Response(200, json={"element": None})
        if "/tools/" in path:
            body = json.loads(request.content)
            assert body == {"input": {"ref": "e1"}, "confirmed": False}
            return httpx.Response(200, json={"status": "ok", "summary": "done"})
        if path.endswith("/context"):
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(204)

    return seen, httpx.MockTransport(handle)


async def test_operator_paths_have_no_trailing_slash() -> None:
    seen, transport = _recorder()
    op = OperatorClient("http://operator:8100/", "t", transport=transport)
    await op.start(session_id="s1")
    await op.run("operate_click", {"ref": "e1"})
    await op.element_at(10, 20)
    await op.stop()
    await op.aclose()
    assert seen == [
        ("POST", "/v1/sandboxes"),
        ("POST", "/v1/sandboxes/sb1/tools/operate_click"),
        ("POST", "/v1/sandboxes/sb1/element-at"),
        ("DELETE", "/v1/sandboxes/sb1"),
    ]


async def test_api_paths() -> None:
    seen, transport = _recorder()
    api = ApiClient("http://api:8000", "t", "s1", transport=transport)
    assert await api.context() == {"session_id": "s1"}
    await api.started()
    await api.ended("agent_ended", None, 2)
    await api.aclose()
    base = "/v1/internal/sessions/s1"
    assert seen == [
        ("GET", f"{base}/context"),
        ("POST", f"{base}/started"),
        ("POST", f"{base}/ended"),
    ]


async def test_operator_start_waits_for_a_busy_operator(monkeypatch) -> None:
    answers = [503, 503, 201]
    naps: list[float] = []

    async def nap(s: float) -> None:
        naps.append(s)

    def handle(request: httpx.Request) -> httpx.Response:
        status = answers.pop(0)
        if status == 503:
            return httpx.Response(503, headers={"retry-after": "2"}, json={"detail": "full"})
        return httpx.Response(status, json={"sandbox_id": "sb1"})

    monkeypatch.setattr("ultrademo_agent.clients.asyncio.sleep", nap)
    op = OperatorClient("http://operator:8100", "t", transport=httpx.MockTransport(handle))
    assert (await op.start(session_id="s1"))["sandbox_id"] == "sb1"
    assert naps == [2.0, 2.0] and op.sandbox_id == "sb1"


async def test_operator_start_gives_up_when_the_operator_stays_full(monkeypatch) -> None:
    calls = 0

    async def nap(s: float) -> None:
        pass

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "full"})

    monkeypatch.setattr("ultrademo_agent.clients.asyncio.sleep", nap)
    op = OperatorClient("http://operator:8100", "t", transport=httpx.MockTransport(handle))
    try:
        await op.start(session_id="s1", busy_retries=2)
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 503
    else:
        raise AssertionError("expected the start to fail")
    assert calls == 3 and op.sandbox_id is None

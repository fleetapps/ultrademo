"""HTTP clients for the api (session context, transcript, actions, cost) and the operator."""

import asyncio
from typing import Any

import httpx
from ultrademo_protocol import ToolResult


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        session_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/v1/internal/sessions/{session_id}",
            headers={"authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=3.0),
            transport=transport,
        )

    async def _post(self, path: str, json: Any) -> None:
        r = await self._http.post(path, json=json)
        r.raise_for_status()

    async def context(self) -> dict[str, Any]:
        r = await self._http.get("/context")
        r.raise_for_status()
        return r.json()

    async def started(self) -> None:
        await self._post("/started", None)

    async def transcript(self, lines: list[dict[str, Any]]) -> None:
        await self._post("/transcript", lines)

    async def actions(self, actions: list[dict[str, Any]]) -> None:
        await self._post("/actions", actions)

    async def cost(self, items: list[dict[str, Any]]) -> None:
        await self._post("/cost", items)

    async def ended(self, reason: str, summary: str | None, participant_turns: int) -> None:
        await self._post(
            "/ended", {"reason": reason, "summary": summary, "participant_turns": participant_turns}
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class OperatorClient:
    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # Paths are absolute under /v1: httpx gives a base URL a trailing slash, so posting to ""
        # would hit /v1/sandboxes/, which FastAPI answers with a redirect.
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/v1",
            headers={"authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30.0, connect=3.0),
            transport=transport,
        )
        self.sandbox_id: str | None = None

    async def start(self, *, busy_retries: int = 5, **body: Any) -> dict[str, Any]:
        # A full operator answers 503 with Retry-After. A sandbox is often about to free up (a call
        # just ended, the reaper is closing one), so wait a little before failing the session.
        for _ in range(busy_retries):
            r = await self._http.post("/sandboxes", json=body)
            if r.status_code != 503:
                break
            await asyncio.sleep(min(float(r.headers.get("retry-after") or 2), 5.0))
        else:
            r = await self._http.post("/sandboxes", json=body)
        r.raise_for_status()
        out = r.json()
        self.sandbox_id = out["sandbox_id"]
        return out

    async def run(
        self, tool: str, tool_input: dict[str, Any], confirmed: bool = False
    ) -> ToolResult:
        assert self.sandbox_id, "sandbox not started"
        r = await self._http.post(
            f"/sandboxes/{self.sandbox_id}/tools/{tool}",
            json={"input": tool_input, "confirmed": confirmed},
        )
        r.raise_for_status()
        return ToolResult.model_validate(r.json())

    async def element_at(self, x: float, y: float) -> dict[str, Any] | None:
        assert self.sandbox_id, "sandbox not started"
        r = await self._http.post(f"/sandboxes/{self.sandbox_id}/element-at", json={"x": x, "y": y})
        r.raise_for_status()
        return r.json()["element"]

    async def stop(self) -> None:
        if self.sandbox_id:
            await self._http.delete(f"/sandboxes/{self.sandbox_id}")
            self.sandbox_id = None

    async def aclose(self) -> None:
        await self._http.aclose()

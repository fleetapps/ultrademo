"""Operator HTTP API, called only by the session agent.

    POST   /v1/sandboxes                     start a browser for a session (optionally streaming it)
    POST   /v1/sandboxes/{id}/tools/{tool}   run one operate_* tool
    POST   /v1/sandboxes/{id}/element-at     the element under a viewport point (viewer pointing)
    DELETE /v1/sandboxes/{id}                stop it

Capacity is explicit: when `max_sandboxes` are running the API answers 503, so the orchestrator
scales out instead of overloading one node. Idle or over-age sandboxes are reaped.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from playwright.async_api import Browser, async_playwright
from pydantic import BaseModel, ConfigDict, Field
from ultrademo_protocol import ToolResult

from ultrademo_operator.sandbox import Sandbox, SandboxConfig
from ultrademo_operator.settings import Settings, get_settings
from ultrademo_operator.streamer import ScreenStreamer, sandbox_token

log = structlog.get_logger()


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    start_url: str
    allowed_domains: list[str] = Field(min_length=1)
    policy: dict[str, Any] = Field(default_factory=dict)
    livekit_room: str | None = None
    locale: str = "en-US"
    width: int = Field(default=1280, ge=640, le=1920)
    height: int = Field(default=720, ge=480, le=1080)
    # False when the player draws the cursor and highlights itself from overlay events.
    draw_overlays: bool = True


class PointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1920)
    y: float = Field(ge=0, le=1080)


class PointOut(BaseModel):
    element: dict[str, Any] | None


class StartOut(BaseModel):
    sandbox_id: str
    url: str
    title: str
    streaming: bool


class ToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, Any] = Field(default_factory=dict)
    # Set by the session agent only after the viewer approved on screen; not part of the model's
    # tool schema, so the model cannot approve its own actions.
    confirmed: bool = False


class _Entry:
    def __init__(self, sandbox: Sandbox, streamer: ScreenStreamer | None) -> None:
        self.sandbox = sandbox
        self.streamer = streamer

    async def close(self) -> None:
        if self.streamer:
            await self.streamer.stop()
        await self.sandbox.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    sandboxes: dict[str, _Entry] = {}
    starting = 0

    async def reaper(app: FastAPI) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for sid, e in list(sandboxes.items()):
                idle = now - e.sandbox.last_used > settings.idle_timeout_s
                old = now - e.sandbox.started_at > settings.max_lifetime_s
                if idle or old:
                    sandboxes.pop(sid, None)
                    log.info("sandbox_reaped", sandbox_id=sid, idle=idle, old=old)
                    await e.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with async_playwright() as pw:
            app.state.pw = pw
            app.state.shared: Browser | None = None
            if settings.share_browser:
                app.state.shared = await pw.chromium.launch(
                    executable_path=settings.chromium_executable, args=["--disable-dev-shm-usage"]
                )
            task = asyncio.create_task(reaper(app))
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(
                    *(e.close() for e in sandboxes.values()), return_exceptions=True
                )
                sandboxes.clear()
                if app.state.shared:
                    await app.state.shared.close()

    app = FastAPI(title="ultrademo operator", version="0.1.0", lifespan=lifespan)

    def auth(request: Request) -> None:
        expected = f"Bearer {settings.internal_token.get_secret_value()}"
        if request.headers.get("authorization") != expected:
            raise HTTPException(401, "invalid internal token")

    Auth = Annotated[None, Depends(auth)]

    @app.post("/v1/sandboxes", status_code=201, response_model=StartOut)
    async def start(body: StartIn, request: Request, _: Auth) -> StartOut:
        nonlocal starting
        if len(sandboxes) + starting >= settings.max_sandboxes:
            raise HTTPException(503, "operator at capacity", headers={"Retry-After": "2"})
        starting += 1
        try:
            config = SandboxConfig(
                session_id=body.session_id,
                start_url=body.start_url,
                allowed_domains=body.allowed_domains,
                policy=body.policy,
                width=body.width,
                height=body.height,
                locale=body.locale,
                draw_overlays=body.draw_overlays,
            )
            try:
                sandbox = await Sandbox.launch(
                    request.app.state.pw,
                    config,
                    settings.chromium_executable,
                    request.app.state.shared,
                )
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            streamer = None
            if body.livekit_room:
                streamer = ScreenStreamer(sandbox.page, body.width, body.height, body.session_id)
                token = sandbox_token(
                    settings.livekit_api_key,
                    settings.livekit_api_secret.get_secret_value(),
                    room=body.livekit_room,
                    session_id=body.session_id,
                    width=body.width,
                    height=body.height,
                )
                try:
                    await streamer.start(settings.livekit_url, token)
                except Exception:
                    await sandbox.close()
                    raise
                sandbox.on_overlay = streamer.publish_overlay
            sid = f"sbx_{uuid.uuid4().hex[:16]}"
            sandboxes[sid] = _Entry(sandbox, streamer)
        finally:
            starting -= 1
        log.info("sandbox_started", sandbox_id=sid, session_id=body.session_id)
        return StartOut(
            sandbox_id=sid,
            url=sandbox.page.url,
            title=await sandbox.page.title(),
            streaming=streamer is not None,
        )

    @app.post("/v1/sandboxes/{sandbox_id}/tools/{tool}", response_model=ToolResult)
    async def run_tool(sandbox_id: str, tool: str, body: ToolIn, _: Auth) -> ToolResult:
        entry = sandboxes.get(sandbox_id)
        if entry is None:
            raise HTTPException(404, "sandbox not found")
        return await entry.sandbox.execute(tool, body.input, confirmed=body.confirmed)

    @app.post("/v1/sandboxes/{sandbox_id}/element-at", response_model=PointOut)
    async def element_at(sandbox_id: str, body: PointIn, _: Auth) -> PointOut:
        """The element under a point on the shared screen (viewport CSS pixels)."""
        entry = sandboxes.get(sandbox_id)
        if entry is None:
            raise HTTPException(404, "sandbox not found")
        el = await entry.sandbox.element_at(body.x, body.y)
        return PointOut(element=el.as_dict() if el else None)

    @app.delete("/v1/sandboxes/{sandbox_id}", status_code=204)
    async def stop(sandbox_id: str, _: Auth) -> None:
        entry = sandboxes.pop(sandbox_id, None)
        if entry:
            await entry.close()

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "sandboxes": len(sandboxes), "capacity": settings.max_sandboxes}

    return app

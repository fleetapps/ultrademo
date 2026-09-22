"""Publish the sandbox's screen into the session's LiveKit room as a screen-share track.

Frames come from Chrome's own screencast (CDP `Page.startScreencast`), which only emits when the
page repaints, so a static screen costs almost nothing. They are published through
`rtc.VideoSource(w, h, is_screencast=True)`, which tells WebRTC to keep text sharp under congestion
by dropping frames instead of resolution (docs/06 §2 "Media").
"""

import asyncio
import base64
import io
import time
from datetime import timedelta

import structlog
from livekit import api, rtc
from PIL import Image
from playwright.async_api import CDPSession, Page

log = structlog.get_logger()

MAX_FPS = 15


def sandbox_token(api_key: str, api_secret: str, *, room: str, session_id: str) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_publish_sources=["screen_share"],
        can_subscribe=False,
        can_publish_data=False,
    )
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(f"sandbox_{session_id}")
        .with_name("Product screen")
        .with_attributes({"ultrademo.kind": "sandbox"})
        .with_grants(grants)
        .with_ttl(timedelta(hours=2))
        .to_jwt()
    )


class ScreenStreamer:
    def __init__(self, page: Page, width: int, height: int) -> None:
        self._page = page
        self._w = width
        self._h = height
        self._room = rtc.Room()
        self._source = rtc.VideoSource(width, height, is_screencast=True)
        self._cdp: CDPSession | None = None
        self._last_sent = 0.0
        self._last_frame: rtc.VideoFrame | None = None
        self._keepalive: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    async def start(self, url: str, token: str) -> None:
        await self._room.connect(url, token)
        track = rtc.LocalVideoTrack.create_video_track("screen", self._source)
        options = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_SCREENSHARE,
            video_encoding=rtc.VideoEncoding(max_bitrate=2_500_000, max_framerate=MAX_FPS),
        )
        await self._room.local_participant.publish_track(track, options)
        self._cdp = await self._page.context.new_cdp_session(self._page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 80, "maxWidth": self._w, "maxHeight": self._h},
        )
        self._keepalive = asyncio.create_task(self._resend_last())

    def _on_frame(self, params: dict) -> None:
        task = asyncio.create_task(self._handle(params))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _handle(self, params: dict) -> None:
        assert self._cdp is not None
        await self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        now = time.monotonic()
        if now - self._last_sent < 1 / MAX_FPS:
            return
        self._last_sent = now
        frame = await asyncio.to_thread(self._decode, params["data"])
        self._last_frame = frame
        self._source.capture_frame(frame)

    def _decode(self, data_b64: str) -> rtc.VideoFrame:
        img = Image.open(io.BytesIO(base64.b64decode(data_b64))).convert("RGBA")
        if img.size != (self._w, self._h):
            img = img.resize((self._w, self._h))
        return rtc.VideoFrame(self._w, self._h, rtc.VideoBufferType.RGBA, img.tobytes())

    async def _resend_last(self) -> None:
        # A viewer who subscribes while the page is static still gets a picture within ~2 s.
        while True:
            await asyncio.sleep(2)
            if self._last_frame is not None and time.monotonic() - self._last_sent > 2:
                self._source.capture_frame(self._last_frame)

    async def stop(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        try:
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 - page may already be gone
            log.debug("screencast_stop_failed")
        await self._source.aclose()
        await self._room.disconnect()

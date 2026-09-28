"""Publish the sandbox's screen into the session's LiveKit room as a screen-share track.

Frames come from Chrome's own screencast (CDP `Page.startScreencast`), which only emits when the
page repaints, so a static screen costs almost nothing. They are published through
`rtc.VideoSource(w, h, is_screencast=True)`, which tells WebRTC to keep text sharp under congestion
by dropping frames instead of resolution (docs/06 §2 "Media").

The same participant publishes overlay events (cursor, highlights) on the events topic, so the
player can draw them over the video in sync with the action (ADR 4), and advertises the viewport
size in the `ultrademo.screen_meta` attribute, which is the coordinate space of those events.
"""

import asyncio
import base64
import io
import json
import time
from datetime import timedelta
from typing import Any

import structlog
from livekit import api, rtc
from PIL import Image
from playwright.async_api import CDPSession, Page
from ultrademo_protocol import ATTR_KIND, ATTR_SCREEN_META, TOPIC_EVENTS, Envelope, EventType

log = structlog.get_logger()

MAX_FPS = 15


def sandbox_token(
    api_key: str,
    api_secret: str,
    *,
    room: str,
    session_id: str,
    width: int = 1280,
    height: int = 720,
) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_publish_sources=["screen_share"],
        can_subscribe=False,
        can_publish_data=True,  # overlay events
        can_update_own_metadata=False,
    )
    meta = json.dumps({"viewport": {"w": width, "h": height}}, separators=(",", ":"))
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(f"sandbox_{session_id}")
        .with_name("Product screen")
        .with_attributes({ATTR_KIND: "sandbox", ATTR_SCREEN_META: meta})
        .with_grants(grants)
        .with_ttl(timedelta(hours=2))
        .to_jwt()
    )


class ScreenStreamer:
    def __init__(self, page: Page, width: int, height: int, session_id: str = "") -> None:
        self._page = page
        self._session_id = session_id
        self._seq = 0
        self._w = width
        self._h = height
        self._room = rtc.Room()
        self._source = rtc.VideoSource(width, height, is_screencast=True)
        self._cdp: CDPSession | None = None
        self._last_sent = 0.0
        self._last_frame: rtc.VideoFrame | None = None
        self._keepalive: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        # The newest frame not yet published. A newer one replaces it, so the pump always ends on
        # the page's final paint instead of dropping it for arriving too soon after the last send.
        self._latest: str | None = None
        self._wake = asyncio.Event()
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
        self._pump_task = asyncio.create_task(self._pump())
        self._keepalive = asyncio.create_task(self._resend_last())

    async def publish_overlay(self, type_: EventType, payload: dict[str, Any]) -> None:
        """Send one overlay event to the room (reliable, so a highlight is never lost)."""
        self._seq += 1
        env = Envelope(type=type_, seq=self._seq, session_id=self._session_id, **payload)
        await self._room.local_participant.publish_data(
            env.encode(), reliable=True, topic=TOPIC_EVENTS
        )

    def _on_frame(self, params: dict) -> None:
        self._latest = params["data"]
        self._wake.set()
        task = asyncio.create_task(self._ack(params["sessionId"]))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _ack(self, frame_session_id: int) -> None:
        assert self._cdp is not None
        await self._cdp.send("Page.screencastFrameAck", {"sessionId": frame_session_id})

    async def _pump(self) -> None:
        """Publish at most MAX_FPS frames a second, always including the last one of a burst.

        Chrome only sends a frame when the page repaints. Dropping a frame for arriving too soon
        would leave the viewer on an earlier paint (the keepalive resends it) until the page
        happens to repaint again, so a frame that arrives early waits instead and newer ones
        replace it.
        """
        while True:
            await self._wake.wait()
            self._wake.clear()
            wait = self._last_sent + 1 / MAX_FPS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            data, self._latest = self._latest, None
            if data is None:
                continue
            frame = await asyncio.to_thread(self._decode, data)
            self._last_sent = time.monotonic()
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
        for task in (self._keepalive, self._pump_task):
            if task:
                task.cancel()
        try:
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 - page may already be gone
            log.debug("screencast_stop_failed")
        await self._source.aclose()
        await self._room.disconnect()

"""The screen streamer's frame pacing, with Chrome's screencast and the LiveKit source faked out."""

import asyncio
import base64
import io
from typing import Any

from PIL import Image
from ultrademo_operator import streamer as streamer_mod
from ultrademo_operator.streamer import ScreenStreamer


def _jpeg(color: tuple[int, int, int]) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, "JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode()


class FakeCdp:
    def __init__(self) -> None:
        self.acks: list[int] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method == "Page.screencastFrameAck":
            self.acks.append(params["sessionId"])


class FakeSource:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def capture_frame(self, frame: Any) -> None:
        self.frames.append(bytes(frame.data))


def _red(frame: bytes) -> int:
    return frame[0]  # RGBA: the first pixel's red channel


async def test_the_last_frame_of_a_burst_is_published(monkeypatch) -> None:
    monkeypatch.setattr(streamer_mod, "MAX_FPS", 10)
    s = ScreenStreamer(None, 64, 36)  # type: ignore[arg-type]
    s._cdp = FakeCdp()  # type: ignore[assignment]
    s._source = FakeSource()  # type: ignore[assignment]
    pump = asyncio.create_task(s._pump())
    try:
        # A click's repaints: three frames well inside one 100 ms slot, then the page is static.
        for i, red in enumerate((10, 120, 250)):
            s._on_frame({"sessionId": i, "data": _jpeg((red, 0, 0))})
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)
    finally:
        pump.cancel()

    assert s._cdp.acks == [0, 1, 2]  # every frame is acked, so Chrome keeps sending
    frames = s._source.frames
    assert 1 <= len(frames) <= 2  # paced, not one publish per paint
    assert abs(_red(frames[-1]) - 250) < 8  # the viewer ends on the final paint
    assert abs(_red(bytes(s._last_frame.data)) - 250) < 8  # and the keepalive resends that one

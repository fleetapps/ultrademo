"""The job entrypoint's wiring order, with the api, operator and voice pipeline faked out.

livekit-agents 1.8 builds RoomIO's transcription output before AgentSession.start connects the
room, and with a participant identity that output reads room.local_participant, which raises until
the room is connected. So the entrypoint must connect first.
"""

import json
from typing import Any

import pytest
from livekit.plugins import elevenlabs, silero
from ultrademo_agent import worker


class _Stop(Exception):
    pass


class FakeApi:
    def __init__(self, *args: Any) -> None:
        pass

    async def context(self) -> dict[str, Any]:
        return {
            "agent_version": {"system_prompt": "You are Ava."},
            "product": {
                "name": "Acme CRM",
                "base_url": "https://crm.example/",
                "allowed_domains": ["crm.example"],
            },
            "max_duration_s": 60,
        }


class FakeOperator:
    def __init__(self, *args: Any) -> None:
        pass

    async def start(self, **body: Any) -> dict[str, Any]:
        return {"sandbox_id": "sb_1"}


class FakeRoom:
    name = "ud_room"


class FakeJob:
    metadata = json.dumps({"session_id": "s_1"})


class FakeContext:
    def __init__(self) -> None:
        self.job = FakeJob()
        self.room = FakeRoom()
        self.connected = False

    def add_shutdown_callback(self, fn: Any) -> None:
        pass

    async def connect(self) -> None:
        self.connected = True


async def test_connects_the_room_before_starting_the_voice_session(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ctx = FakeContext()
    seen: dict[str, bool] = {}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def on(self, event: str) -> Any:
            return lambda fn: fn

        async def start(self, **kwargs: Any) -> None:
            seen["connected"] = ctx.connected
            raise _Stop

    monkeypatch.setattr(worker, "ApiClient", FakeApi)
    monkeypatch.setattr(worker, "OperatorClient", FakeOperator)
    monkeypatch.setattr(worker, "AgentSession", FakeSession)
    monkeypatch.setattr(worker, "build_stt", lambda settings, voice: None)
    monkeypatch.setattr(silero.VAD, "load", staticmethod(lambda: None))
    monkeypatch.setattr(elevenlabs, "TTS", lambda **kwargs: None)
    monkeypatch.setattr(worker.inference, "TurnDetector", lambda: None)

    with pytest.raises(_Stop):
        await worker.entrypoint(ctx)
    assert seen == {"connected": True}


async def test_a_failed_start_is_recorded_as_an_error(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ended: list[str] = []
    callbacks: list[Any] = []

    class Api(FakeApi):
        async def transcript(self, lines: Any) -> None:
            pass

        async def ended(self, reason: str, summary: Any, turns: int) -> None:
            ended.append(reason)

        async def aclose(self) -> None:
            pass

    class BusyOperator(FakeOperator):
        async def start(self, **body: Any) -> dict[str, Any]:
            raise RuntimeError("operator at capacity")

        async def stop(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class Ctx(FakeContext):
        def add_shutdown_callback(self, fn: Any) -> None:
            callbacks.append(fn)

    monkeypatch.setattr(worker, "ApiClient", Api)
    monkeypatch.setattr(worker, "OperatorClient", BusyOperator)
    with pytest.raises(RuntimeError):
        await worker.entrypoint(Ctx())
    for fn in callbacks:
        await fn("job failed")
    assert ended == ["error"]

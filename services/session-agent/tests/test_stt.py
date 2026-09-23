from livekit.plugins import deepgram, elevenlabs
from ultrademo_agent.settings import Settings
from ultrademo_agent.worker import build_stt


def test_deepgram_is_the_default(monkeypatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    s = build_stt(Settings(), {"stt_language": "multi"})
    assert isinstance(s, deepgram.STT) and s.model == "nova-3"


def test_elevenlabs_streams_without_a_deepgram_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("ELEVEN_API_KEY", "el-test")
    settings = Settings(stt_provider="elevenlabs")
    s = build_stt(settings, {"stt_language": "multi"})
    assert isinstance(s, elevenlabs.STT)
    assert s.model == "scribe_v2_realtime" and s.capabilities.streaming
    # Deepgram's "multi" means auto-detect, which Scribe does when given no language.
    assert s._opts.language_code is None
    assert build_stt(settings, {"stt_language": "de"})._opts.language_code == "de"

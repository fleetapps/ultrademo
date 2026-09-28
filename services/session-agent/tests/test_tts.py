from livekit.plugins import elevenlabs
from ultrademo_agent.settings import Settings
from ultrademo_agent.worker import build_tts


def test_voice_streams_pcm_not_the_plugins_32_kbps_mp3(monkeypatch) -> None:
    monkeypatch.setenv("ELEVEN_API_KEY", "el-test")
    t = build_tts(Settings(), {"voice_id": "voice-1", "model": "eleven_flash_v2_5"})
    assert isinstance(t, elevenlabs.TTS)
    assert t._opts.encoding == "pcm_24000" and t.sample_rate == 24000
    assert t._opts.voice_id == "voice-1" and t._opts.model == "eleven_flash_v2_5"

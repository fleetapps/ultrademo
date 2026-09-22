from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ULTRADEMO_AGENT_", env_file=".env", extra="ignore"
    )

    agent_name: str = "ultrademo-session-agent"
    api_url: str = "http://localhost:8000"
    operator_url: str = "http://localhost:8100"
    internal_token: SecretStr = SecretStr("dev-internal-token")

    # Claude. Defaults per docs/04 §2: Opus 5, adaptive thinking (its default), low effort for
    # conversational turns. An agent version can override model and effort.
    model: str = "claude-opus-5"
    effort: Effort = "low"
    max_tokens: int = 4096
    max_tool_rounds: int = 12
    # Server-side refusal fallback (`fallbacks: "default"`, beta server-side-fallback-2026-07-01).
    fallbacks: bool = True
    # Clear old tool results once the prompt passes this many tokens (context editing beta).
    clear_tool_results_at_tokens: int = 60_000

    # Voice. Deepgram Nova-3 multilingual STT and ElevenLabs Flash v2.5 TTS (docs/05 §1).
    stt_model: str = "nova-3"
    tts_model: str = "eleven_flash_v2_5"
    tts_voice_id: str = "EXAVITQu4vr4xnSDxMaL"

    confirm_timeout_s: float = 20.0


@lru_cache
def get_settings() -> Settings:
    return Settings()

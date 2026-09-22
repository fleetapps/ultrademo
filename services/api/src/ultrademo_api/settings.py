from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ULTRADEMO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres@localhost:5432/ultrademo"
    db_pool_min: int = 1
    db_pool_max: int = 10

    public_base_url: str = "http://localhost:3000"

    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: SecretStr = SecretStr("secret")
    agent_name: str = "ultrademo-session-agent"
    viewer_token_ttl_s: int = 900

    # Shared secret for session-agent -> api calls. Never sent to browsers.
    internal_token: SecretStr = SecretStr("dev-internal-token")

    # Verified API keys are cached briefly so argon2 runs once per key per window, not per request.
    api_key_cache_ttl_s: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ULTRADEMO_OPERATOR_", env_file=".env", extra="ignore"
    )

    internal_token: SecretStr = SecretStr("dev-internal-token")
    # Point at a preinstalled Chromium when the Playwright-managed build is absent.
    chromium_executable: str | None = None
    # One Chromium per sandbox isolates tenants at the process level (docs/06 §2). Setting this to
    # true shares one browser across sandboxes (separate contexts) to save memory in dev.
    share_browser: bool = False
    max_sandboxes: int = 4
    idle_timeout_s: int = 600
    max_lifetime_s: int = 7_200

    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: SecretStr = SecretStr("secret")


@lru_cache
def get_settings() -> Settings:
    return Settings()

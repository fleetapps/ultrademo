from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ULTRADEMO_", env_file=".env", extra="ignore")

    # "production" refuses to start with any of the development secrets below.
    environment: Literal["development", "production"] = "development"

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
    # Signs the per-session receipt the player uses to report CTA clicks and feedback.
    receipt_secret: SecretStr = SecretStr("dev-receipt-secret")
    # How long after a session starts its receipt may still report events (post-call screen).
    receipt_ttl_s: int = 86_400

    # Verified API keys are cached briefly so argon2 runs once per key per window, not per request.
    api_key_cache_ttl_s: int = 60

    # Outbox relay (`python -m ultrademo_api.relay`): webhooks, Slack alerts and follow-up email.
    relay_poll_s: float = 1.0
    relay_batch: int = 100
    relay_concurrency: int = 10
    webhook_timeout_s: float = 10.0
    # Webhook and Slack URLs must be https and resolve to public addresses. Tests and local
    # receivers can turn this off; production never should.
    allow_private_targets: bool = False
    # Follow-up email. Empty `smtp_url` turns email off. `smtp://user:pass@host:587` uses STARTTLS;
    # `smtps://...:465` uses implicit TLS.
    smtp_url: SecretStr = SecretStr("")
    email_from: str = ""

    def check_secrets(self) -> None:
        """Development defaults are public; a production api must be given its own secrets."""
        if self.environment != "production":
            return
        defaults = {
            "ULTRADEMO_INTERNAL_TOKEN": (self.internal_token, "dev-internal-token"),
            "ULTRADEMO_RECEIPT_SECRET": (self.receipt_secret, "dev-receipt-secret"),
            "ULTRADEMO_LIVEKIT_API_SECRET": (self.livekit_api_secret, "secret"),
        }
        weak = [name for name, (v, d) in defaults.items() if v.get_secret_value() in ("", d)]
        if weak:
            raise RuntimeError(f"Set real values for {', '.join(weak)} in production")
        if self.allow_private_targets:
            raise RuntimeError("ULTRADEMO_ALLOW_PRIVATE_TARGETS must be off in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()

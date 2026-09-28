"""Outbound webhooks: the event envelope customers receive and how it is signed.

Signing follows the Standard Webhooks spec (standardwebhooks.com), so receivers can verify with an
off-the-shelf library: the secret is `whsec_<base64 key>`, and each request carries
`webhook-id`, `webhook-timestamp` and `webhook-signature: v1,<base64 HMAC-SHA256>` over
`<id>.<timestamp>.<body>`. `webhook-id` is stable across retries, so receivers dedupe on it.
"""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

SECRET_PREFIX = "whsec_"  # noqa: S105 - a format marker, not a secret
# Receivers should reject timestamps further than this from their clock (replay protection).
DEFAULT_TOLERANCE_S = 5 * 60


class WebhookTopic(StrEnum):
    """Every topic the api writes to the outbox; each is deliverable as a webhook `type`."""

    CONTEXT_LINK_CREATED = "context_link.created"
    SESSION_CREATED = "session.created"
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    CTA_CLICKED = "cta.clicked"
    SESSION_FEEDBACK = "session.feedback"


class WebhookEvent(BaseModel):
    """The JSON body of every webhook request."""

    id: str  # the outbox row id: the same event has the same id at every endpoint
    type: WebhookTopic
    created_at: datetime
    org_id: str
    data: dict[str, Any]


def new_secret() -> str:
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(24)).decode()


def _key(secret: str) -> bytes:
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("webhook secrets start with whsec_")
    return base64.b64decode(secret.removeprefix(SECRET_PREFIX))


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def headers(secret: str, msg_id: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, msg_id, ts, body),
    }


def verify(
    secret: str,
    request_headers: dict[str, str],
    body: bytes,
    *,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    now: float | None = None,
) -> bool:
    """What a receiver runs. Header names are matched case-insensitively."""
    h = {k.lower(): v for k, v in request_headers.items()}
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not msg_id or not ts or not sigs or not ts.isdigit():
        return False
    if abs((time.time() if now is None else now) - int(ts)) > tolerance_s:
        return False
    expected = sign(secret, msg_id, int(ts), body)
    # The header may carry several space-separated signatures during a secret rotation.
    return any(hmac.compare_digest(s, expected) for s in sigs.split())

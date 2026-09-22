"""Opaque keyset cursors over (created_at, id), newest first."""

import base64
import json
from datetime import datetime
from uuid import UUID

from ultrademo_api.errors import ApiError


def encode_cursor(created_at: datetime, id_: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(id_)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ts, id_ = json.loads(raw)
        return datetime.fromisoformat(ts), UUID(id_)
    except (ValueError, TypeError) as e:
        raise ApiError(400, "invalid_cursor", "Cursor is malformed") from e

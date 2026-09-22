"""API keys: `ud_<prefix>_<secret>`, shown once, stored as an argon2id hash.

The prefix is a public lookup handle (unique index); the secret never leaves the caller. argon2 is
deliberately slow, so a verified key is cached in-process for a short TTL keyed by a SHA-256 of the
full key. Revocation therefore takes effect within `api_key_cache_ttl_s`.
"""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def random_token(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


@dataclass(frozen=True)
class NewApiKey:
    prefix: str
    plaintext: str
    key_hash: str


def generate_api_key() -> NewApiKey:
    prefix = random_token(10)
    plaintext = f"ud_{prefix}_{random_token(32)}"
    return NewApiKey(prefix=prefix, plaintext=plaintext, key_hash=_hasher.hash(plaintext))


def parse_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_")
    if len(parts) != 3 or parts[0] != "ud" or len(parts[1]) != 10 or len(parts[2]) != 32:
        return None
    return parts[1]


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    try:
        return _hasher.verify(key_hash, plaintext)
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True)
class Principal:
    org_id: UUID
    key_id: UUID
    scopes: frozenset[str]

    def can(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


class KeyCache:
    def __init__(self, ttl_s: int) -> None:
        self._ttl = ttl_s
        self._items: dict[str, tuple[float, Principal]] = {}

    @staticmethod
    def _k(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode()).hexdigest()

    def get(self, plaintext: str) -> Principal | None:
        hit = self._items.get(self._k(plaintext))
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def put(self, plaintext: str, principal: Principal) -> None:
        if len(self._items) > 10_000:
            self._items.clear()
        self._items[self._k(plaintext)] = (time.monotonic() + self._ttl, principal)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

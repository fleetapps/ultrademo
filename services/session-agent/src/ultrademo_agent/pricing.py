"""Rate card for the per-session cost ledger (docs/06 §9). USD per million tokens.

Cache writes are the 5-minute TTL price (1.25x input); cache reads are 0.1x input.
"""

from dataclasses import dataclass
from decimal import Decimal

RATE_CARD_VERSION = "2026-09-22"


@dataclass(frozen=True)
class ModelRates:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal


_OPUS_5 = ModelRates(Decimal("5"), Decimal("25"), Decimal("0.50"), Decimal("6.25"))

RATES: dict[str, ModelRates] = {
    "claude-opus-5": _OPUS_5,
    # The refusal fallback may serve a turn on Opus 4.8, billed at its own (same list) rates.
    "claude-opus-4-8": _OPUS_5,
}

_M = Decimal(1_000_000)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens

    def ledger(self, model: str) -> list[dict]:
        r = RATES.get(model, _OPUS_5)
        rows = [
            ("llm_in", self.input_tokens, r.input),
            ("llm_out", self.output_tokens, r.output),
            ("llm_cache_read", self.cache_read_input_tokens, r.cache_read),
            ("llm_cache_write", self.cache_creation_input_tokens, r.cache_write_5m),
        ]
        return [
            {
                "item": item,
                "qty": str(qty),
                "usd": str((Decimal(qty) * rate / _M).quantize(Decimal("0.000001"))),
                "rate_card_version": RATE_CARD_VERSION,
            }
            for item, qty, rate in rows
            if qty
        ]

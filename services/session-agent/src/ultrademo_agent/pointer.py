"""The viewer points at the shared screen and asks "what's this?" (docs/02 §2 #3).

The player calls the `ultrademo.pointer` RPC on the agent with a point in viewport pixels. The
operator hit-tests its accessibility snapshot, so the answer is the same element (role, name, ref)
the model sees and can act on. The element goes back to the viewer, and a note goes to the brain.
A `click` is an explicit question, so it also starts a turn, as if the viewer had asked aloud.
"""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

log = structlog.get_logger()

MIN_INTERVAL_S = 0.75
# A click starts a (billed) model turn, so clicks are spaced further apart than plain points.
MIN_CLICK_INTERVAL_S = 3.0
# The largest sandbox viewport the operator allows (its StartIn width/height bounds).
MAX_X, MAX_Y = 1920, 1080


class _Operator(Protocol):
    async def element_at(self, x: float, y: float) -> dict[str, Any] | None: ...


class _Brain(Protocol):
    def note(self, text: str) -> None: ...


class Pointer:
    def __init__(
        self,
        operator: _Operator,
        brain: _Brain,
        ask: Callable[[str], Awaitable[None]],
        *,
        viewer_identity: str,
    ) -> None:
        self._operator = operator
        self._brain = brain
        self._ask = ask
        self._viewer = viewer_identity
        self._last = 0.0
        self._last_click = float("-inf")

    async def handle(self, caller_identity: str, payload: str) -> str:
        """RPC handler body. Returns `{"element": {...} | null}`; never raises to the caller."""
        if caller_identity != self._viewer:
            return json.dumps({"element": None, "error": "not_allowed"})
        try:
            data = json.loads(payload)
            x, y = float(data["x"]), float(data["y"])
            kind = data.get("kind", "point")
        except (ValueError, KeyError, TypeError):
            return json.dumps({"element": None, "error": "bad_request"})
        if kind not in ("point", "click") or not (0 <= x <= MAX_X and 0 <= y <= MAX_Y):
            return json.dumps({"element": None, "error": "bad_request"})
        now = time.monotonic()
        if now - self._last < MIN_INTERVAL_S or (
            kind == "click" and now - self._last_click < MIN_CLICK_INTERVAL_S
        ):
            return json.dumps({"element": None, "error": "too_fast"})
        self._last = now
        if kind == "click":
            self._last_click = now

        try:
            element = await self._operator.element_at(x, y)
        except Exception as e:  # noqa: BLE001 - the sandbox may be gone; the viewer just sees nothing
            log.warning("pointer_lookup_failed", error=str(e)[:200])
            return json.dumps({"element": None, "error": "unavailable"})
        if element is None:
            return json.dumps({"element": None})

        role, name, ref = element.get("role", ""), element.get("name", ""), element.get("ref", "")
        what = f'{role} "{name}"' if name else role
        self._brain.note(
            f"[The viewer is pointing at the {what} (ref {ref}) on the shared screen.]"
        )
        if kind == "click":
            await self._ask(f"What is this? (pointing at the {what})")
        return json.dumps({"element": element})

"""Demo pacing: the platform, not the viewer, starts a turn when the demo needs moving along.

Two cases, both delivered to the brain as notes (see Brain.note), never as fake viewer speech:
- Time is running out: the agent is told to wrap up before the session limit, so the call ends
  with a recap and a call to action instead of the worker's fixed goodbye.
- The viewer went quiet: AgentSession marks the user "away" after `user_away_timeout` of silence
  on both sides, and the agent checks in once. The user stays "away" until they speak, so a
  viewer who has stepped away is not talked at repeatedly.

Independent of LiveKit, so it is unit-tested with fakes; the worker wires the callbacks.
"""

import asyncio
import math
from collections.abc import Callable
from typing import Protocol

import structlog

from ultrademo_agent.prompt import QUIET_NUDGE, wrap_up_note

log = structlog.get_logger()


class _Brain(Protocol):
    ended: bool

    def note(self, text: str) -> None: ...


class Pacing:
    def __init__(
        self,
        brain: _Brain,
        *,
        start_turn: Callable[[], None],
        agent_idle: Callable[[], bool],
        max_duration_s: float,
        wrap_up_before_s: float,
    ) -> None:
        self._brain = brain
        self._start_turn = start_turn
        self._agent_idle = agent_idle
        self._max_duration_s = max_duration_s
        self._wrap_up_before_s = wrap_up_before_s
        self._user_state = "listening"

    async def wrap_up_timer(self) -> None:
        """Run for the session; cancel it when the session ends."""
        before = self._wrap_up_before_s
        if before <= 0 or self._max_duration_s < 2 * before:
            return
        await asyncio.sleep(self._max_duration_s - before)
        if self._brain.ended:
            return
        self._brain.note(wrap_up_note(max(1, math.ceil(before / 60))))
        # While the viewer talks, the note rides their turn; otherwise say it now.
        if self._agent_idle() and self._user_state != "speaking":
            self._start_turn()
        log.info("pacing_wrap_up", seconds_left=before)

    def user_state_changed(self, new_state: str) -> None:
        self._user_state = new_state
        if new_state != "away" or self._brain.ended or not self._agent_idle():
            return
        self._brain.note(QUIET_NUDGE)
        self._start_turn()
        log.info("pacing_quiet_nudge")

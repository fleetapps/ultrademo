import asyncio

from ultrademo_agent.pacing import Pacing
from ultrademo_agent.prompt import QUIET_NUDGE


class FakeBrain:
    def __init__(self) -> None:
        self.ended = False
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)


def pacing(brain, *, idle=True, max_s=600.0, before=120.0):
    turns: list[int] = []
    p = Pacing(
        brain,
        start_turn=lambda: turns.append(1),
        agent_idle=lambda: idle,
        max_duration_s=max_s,
        wrap_up_before_s=before,
    )
    return p, turns


def test_quiet_viewer_gets_one_check_in_per_away():
    b = FakeBrain()
    p, turns = pacing(b)
    p.user_state_changed("listening")
    assert turns == [] and b.notes == []
    p.user_state_changed("away")
    assert turns == [1] and b.notes == [QUIET_NUDGE]


def test_no_check_in_while_the_agent_is_busy_or_after_the_end():
    b = FakeBrain()
    p, turns = pacing(b, idle=False)
    p.user_state_changed("away")
    b2 = FakeBrain()
    b2.ended = True
    p2, turns2 = pacing(b2)
    p2.user_state_changed("away")
    assert turns == turns2 == [] and b.notes == b2.notes == []


async def test_wrap_up_note_before_the_limit():
    b = FakeBrain()
    p, turns = pacing(b, max_s=0.2, before=0.1)
    await asyncio.wait_for(p.wrap_up_timer(), 1)
    assert turns == [1] and "About 1 minute left" in b.notes[0]


async def test_wrap_up_rides_the_viewer_turn_while_they_talk():
    b = FakeBrain()
    p, turns = pacing(b, max_s=0.2, before=0.1)
    p.user_state_changed("speaking")
    await asyncio.wait_for(p.wrap_up_timer(), 1)
    assert turns == [] and len(b.notes) == 1


async def test_short_sessions_and_disabled_wrap_up_skip_it():
    for max_s, before in [(100.0, 120.0), (600.0, 0.0)]:
        b = FakeBrain()
        p, turns = pacing(b, max_s=max_s, before=before)
        await asyncio.wait_for(p.wrap_up_timer(), 1)
        assert turns == [] and b.notes == []

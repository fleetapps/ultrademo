import json

from ultrademo_agent import pointer as pointer_mod
from ultrademo_agent.pointer import Pointer

LINK = {"ref": "e5", "role": "link", "name": "Deals", "bbox": {"x": 1, "y": 2, "w": 3, "h": 4}}


class Op:
    def __init__(self, element=LINK, fail=False):
        self.element, self.fail, self.calls = element, fail, []

    async def element_at(self, x, y):
        self.calls.append((x, y))
        if self.fail:
            raise RuntimeError("sandbox gone")
        return self.element


class BrainNotes:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(text)


def make(op=None):
    asked: list[str] = []

    async def ask(text):
        asked.append(text)

    brain = BrainNotes()
    return Pointer(op or Op(), brain, ask, viewer_identity="viewer_s1"), brain, asked


async def test_point_notes_the_element_and_click_asks(monkeypatch):
    monkeypatch.setattr(pointer_mod, "MIN_INTERVAL_S", 0)
    p, brain, asked = make()
    out = json.loads(await p.handle("viewer_s1", json.dumps({"x": 10, "y": 20, "kind": "point"})))
    assert out == {"element": LINK}
    assert brain.notes == [
        '[The viewer is pointing at the link "Deals" (ref e5) on the shared screen.]'
    ]
    assert asked == []

    await p.handle("viewer_s1", json.dumps({"x": 10, "y": 20, "kind": "click"}))
    assert asked == ['What is this? (pointing at the link "Deals")']


async def test_pointer_rejects_bad_calls(monkeypatch):
    monkeypatch.setattr(pointer_mod, "MIN_INTERVAL_S", 0)
    op = Op()
    p, brain, asked = make(op)
    assert json.loads(await p.handle("sandbox_s1", '{"x":1,"y":1}'))["error"] == "not_allowed"
    assert json.loads(await p.handle("viewer_s1", "not json"))["error"] == "bad_request"
    assert json.loads(await p.handle("viewer_s1", '{"x":1}'))["error"] == "bad_request"
    bad_kind = '{"x":1,"y":1,"kind":"drag"}'
    assert json.loads(await p.handle("viewer_s1", bad_kind))["error"] == "bad_request"
    assert json.loads(await p.handle("viewer_s1", '{"x":-5,"y":1}'))["error"] == "bad_request"
    # Beyond the largest viewport the operator allows.
    assert json.loads(await p.handle("viewer_s1", '{"x":1921,"y":1}'))["error"] == "bad_request"
    assert op.calls == [] and brain.notes == [] and asked == []

    failing = make(Op(fail=True))[0]
    assert json.loads(await failing.handle("viewer_s1", '{"x":1,"y":1}'))["error"] == "unavailable"
    nothing = make(Op(element=None))[0]
    assert json.loads(await nothing.handle("viewer_s1", '{"x":1,"y":1}')) == {"element": None}


async def test_pointer_is_throttled():
    p, brain, _ = make()
    await p.handle("viewer_s1", '{"x":1,"y":1}')
    out = json.loads(await p.handle("viewer_s1", '{"x":1,"y":1}'))
    assert out["error"] == "too_fast" and len(brain.notes) == 1


async def test_clicks_are_spaced_further_apart(monkeypatch):
    monkeypatch.setattr(pointer_mod, "MIN_INTERVAL_S", 0)
    p, brain, asked = make()
    click = json.dumps({"x": 10, "y": 20, "kind": "click"})
    assert "error" not in json.loads(await p.handle("viewer_s1", click))
    assert json.loads(await p.handle("viewer_s1", click))["error"] == "too_fast"
    # Plain points still go through between clicks.
    point = json.dumps({"x": 10, "y": 20, "kind": "point"})
    assert "error" not in json.loads(await p.handle("viewer_s1", point))
    assert len(asked) == 1

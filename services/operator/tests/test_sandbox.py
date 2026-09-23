import re

import pytest
from ultrademo_operator.policy import Policy
from ultrademo_operator.sandbox import Sandbox, SandboxConfig, host_allowed, parse_elements
from ultrademo_protocol import PolicyClass


def ref_for(snapshot: str, role: str, name: str) -> str:
    m = re.search(rf'{role} "{re.escape(name)}"[^\n]*?\[ref=(\w+)\]', snapshot)
    assert m, f"{role} {name!r} not in snapshot:\n{snapshot}"
    return m.group(1)


@pytest.fixture
async def sandbox(browser, site_url):
    pw, b = browser
    sb = await Sandbox.launch(
        pw,
        SandboxConfig(
            session_id="s1", start_url=f"{site_url}/index.html", allowed_domains=["127.0.0.1"]
        ),
        shared_browser=b,
    )
    yield sb
    await sb.close()


def test_host_allowed():
    assert host_allowed("https://app.acme.example/x", ["acme.example"])
    assert host_allowed("https://acme.example", ["acme.example"])
    assert not host_allowed("https://acme.example.evil.io", ["acme.example"])
    assert not host_allowed("javascript:alert(1)", ["acme.example"])
    assert not host_allowed("file:///etc/passwd", ["acme.example"])


def test_parse_elements():
    els = parse_elements(
        '- button "Save \\"deal\\"" [ref=e8] [box=289,84,75,21]\n- textbox "Name" [ref=e6]'
    )
    assert els["e8"].name == 'Save "deal"' and els["e8"].box == (289, 84, 75, 21)
    assert els["e6"].role == "textbox" and els["e6"].box is None


def test_parse_elements_marks_iframe_contents():
    # After a navigation the main frame's refs carry a prefix too, so only nesting tells.
    els = parse_elements(
        "- main [ref=f2e1] [box=0,0,800,600]:\n"
        "  - iframe [ref=f2e2] [box=10,10,300,200]:\n"
        '    - button "Pay" [ref=f3e1] [box=5,5,40,20]\n'
        '  - link "Deals" [ref=f2e3] [box=8,8,36,17]\n'
    )
    assert not els["f2e1"].in_frame and not els["f2e2"].in_frame
    assert els["f3e1"].in_frame
    assert not els["f2e3"].in_frame


def test_policy_classes():
    p = Policy.from_agent({"allowed": [r"^save deal$"], "blocked": [r"\bforecast\b"]})
    assert p.classify("operate_click", "button", "Delete deal") == PolicyClass.CONFIRM
    assert p.classify("operate_click", "button", "Save deal") == PolicyClass.ALLOWED  # override
    assert p.classify("operate_click", "link", "Billing") == PolicyClass.BLOCKED
    assert p.classify("operate_click", "button", "Show forecast") == PolicyClass.BLOCKED
    assert p.classify("operate_hover", "link", "Billing") == PolicyClass.ALLOWED
    assert (
        p.classify("operate_type", "searchbox", "Search deals", submits=True) == PolicyClass.ALLOWED
    )


async def test_observe_click_and_type(sandbox):
    obs = await sandbox.execute("operate_observe", {})
    assert obs.status == "ok" and obs.title == "Acme CRM · Deals"
    search = ref_for(obs.snapshot, "searchbox", "Search deals")
    r = await sandbox.execute("operate_type", {"ref": search, "text": "Hooli", "submit": True})
    assert r.status == "ok", r.summary
    assert "Result for Hooli" in r.snapshot

    link = ref_for(r.snapshot, "link", "Deals")
    r = await sandbox.execute("operate_click", {"ref": link})
    assert r.status == "ok"
    deal = ref_for(r.snapshot, "link", "Hooli renewal")
    r = await sandbox.execute("operate_click", {"ref": deal})
    assert r.status == "ok" and r.title == "Acme CRM · Hooli renewal"
    assert r.element["role"] == "link" and set(r.element["bbox"]) == {"x", "y", "w", "h"}


async def test_confirmation_gate(sandbox, site_url):
    r = await sandbox.execute("operate_navigate", {"url": "/deal.html"})
    assert r.status == "ok"
    stage = ref_for(r.snapshot, "combobox", "Stage")
    r = await sandbox.execute("operate_select", {"ref": stage, "value": "Won"})
    assert r.status == "ok", r.summary

    delete = ref_for(r.snapshot, "button", "Delete deal")
    gated = await sandbox.execute("operate_click", {"ref": delete})
    assert gated.status == "needs_confirmation" and gated.policy == PolicyClass.CONFIRM
    assert "Deleted" not in (await sandbox.execute("operate_observe", {})).snapshot

    save = ref_for(r.snapshot, "button", "Save deal")
    r = await sandbox.execute("operate_click", {"ref": save}, confirmed=True)
    assert r.status == "ok"
    assert "status: Saved Won" in r.snapshot or "Saved Won" in r.snapshot


async def test_blocked_and_offsite(sandbox):
    obs = await sandbox.execute("operate_observe", {})
    r = await sandbox.execute("operate_click", {"ref": ref_for(obs.snapshot, "link", "Billing")})
    assert r.status == "blocked"
    r = await sandbox.execute("operate_navigate", {"url": "https://evil.example/"})
    assert r.status == "blocked"
    # A click on an off-domain link is aborted by the route guard; the page stays on the product.
    r = await sandbox.execute(
        "operate_click", {"ref": ref_for(obs.snapshot, "link", "Partner portal")}
    )
    assert sandbox.page.url.startswith("http://127.0.0.1")


async def test_popup_folds_into_main_page(sandbox):
    obs = await sandbox.execute("operate_observe", {})
    await sandbox.execute("operate_click", {"ref": ref_for(obs.snapshot, "link", "Open report")})
    await sandbox.page.wait_for_url("**/report.html", timeout=5000)
    assert len(sandbox.page.context.pages) == 1


async def test_errors_are_results_not_exceptions(sandbox):
    r = await sandbox.execute("operate_click", {"ref": "e999"})
    assert r.status == "error" and "operate_observe" in r.summary
    r = await sandbox.execute("operate_click", {"ref": "body > div"})
    assert r.status == "error" and r.summary.startswith("Invalid input")
    r = await sandbox.execute("ui_show_cta", {"kind": "book", "label": "x"})
    assert r.status == "error"


async def test_screenshot_and_highlight(sandbox):
    obs = await sandbox.execute("operate_observe", {"screenshot": True})
    assert obs.screenshot_jpeg_b64 and len(obs.screenshot_jpeg_b64) > 1000
    r = await sandbox.execute(
        "operate_highlight",
        {"ref": ref_for(obs.snapshot, "heading", "Deals"), "label": "Your pipeline"},
    )
    assert r.status == "ok"
    assert await sandbox.page.evaluate("typeof window.__ultrademo.highlight") == "function"


async def test_overlay_events_and_pointing(sandbox):
    events: list[tuple[str, dict]] = []

    async def sink(type_, payload):
        events.append((type_.value, payload))

    sandbox.on_overlay = sink
    obs = await sandbox.execute("operate_observe", {})
    link = ref_for(obs.snapshot, "link", "Deals")
    await sandbox.execute("operate_highlight", {"ref": link, "label": "Your pipeline"})
    r = await sandbox.execute("operate_click", {"ref": link})
    assert r.status == "ok"

    kinds = [(t, p.get("kind")) for t, p in events]
    assert kinds == [
        ("overlay.highlight", None),
        ("overlay.cursor", "move"),
        ("overlay.cursor", "click"),
    ]
    hl = events[0][1]
    assert hl["label"] == "Your pipeline" and hl["ttl_ms"] == 4000
    assert (hl["screen_w"], hl["screen_h"]) == (1280, 720)
    bbox = hl["bbox"]
    # The cursor lands in the middle of the highlighted element, in the same coordinate space.
    cur = events[1][1]
    assert bbox["x"] <= cur["x"] <= bbox["x"] + bbox["w"]
    assert bbox["y"] <= cur["y"] <= bbox["y"] + bbox["h"]

    # Pointing at that spot returns the element the model would address, with a usable ref.
    el = await sandbox.element_at(cur["x"], cur["y"])
    assert el is not None and el.role == "link" and el.name == "Deals"
    again = await sandbox.execute("operate_click", {"ref": el.ref})
    assert again.status == "ok"
    assert await sandbox.element_at(-50, -50) is None

    # A failing sink never breaks the action.
    async def broken(type_, payload):
        raise RuntimeError("room gone")

    sandbox.on_overlay = broken
    r = await sandbox.execute("operate_navigate", {"url": "/index.html"})
    assert r.status == "ok"

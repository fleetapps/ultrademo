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

"""One live browser for one demo session.

The agent sees the product through Playwright's AI-mode ARIA snapshot (`aria_snapshot(mode="ai",
boxes=True)`, Playwright >= 1.60): a compact YAML tree where each interactive element carries a
`[ref=eN]` and its viewport box. Actions address elements by that ref through the `aria-ref=eN`
selector, so the model never writes CSS selectors and never guesses coordinates.

Safety lives here, not in the prompt:
- Top-level navigations outside the product's allowed domains are aborted.
- New tabs and popups are folded back into the one visible page (or closed if off-domain).
- Every action on an element is classified by `Policy` before it runs.
"""

import asyncio
import base64
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from ultrademo_protocol import EventType, PolicyClass, ToolResult, parse_tool_input
from ultrademo_protocol.tools import OPERATOR_TOOLS

from ultrademo_operator.policy import Policy

log = structlog.get_logger()

_OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text()

# Receives overlay events (`overlay.cursor`, `overlay.highlight`, `overlay.clear`) for the player to
# draw (ADR 4). The screen streamer publishes them into the LiveKit room.
OverlaySink = Callable[[EventType, dict[str, Any]], Awaitable[None]]

# Roles a viewer can meaningfully point at, most specific first when boxes nest.
_POINTABLE_SKIP = {"generic", "none", "presentation", "document", "main", "region", "group"}
HIGHLIGHT_TTL_MS = 4_000

# `- button "Save deal" [ref=e8] [box=289,84,75,21]` and variants without a name or box.
_LINE_RE = re.compile(
    r'^\s*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?.*?\[ref=(?P<ref>(?:f\d+)?e\d+)\]'
    r"(?:.*?\[box=(?P<box>-?\d+,-?\d+,\d+,\d+)\])?"
)

MAX_SNAPSHOT_CHARS = 16_000

# After an action, the page counts as settled once its DOM and the data requests the action started
# have both been still this long. A few frames covers a re-render; a fetch the action started keeps
# it waiting until the response has been rendered.
SETTLE_QUIET_MS = 100
# Typing without submitting: search-as-you-type fields commonly debounce 300 ms before they fetch.
SETTLE_TYPING_QUIET_MS = 400
# Upper bound for pages that never go quiet (live polling, tickers, animated DOM).
SETTLE_MAX_MS = 1_500
_SETTLE_POLL_S = 0.05
_DATA_REQUESTS = {"fetch", "xhr"}
_REQUEST_FORGET_S = 60

# Records when the document last changed, from the first moment it exists. Mutations inside the
# overlay's closed shadow root never reach a document observer, and its host is skipped.
_MUTATION_CLOCK_JS = """(() => {
  const mark = (records) => {
    const overlay = (n) => n.nodeName === "ULTRADEMO-OVERLAY";
    const own = (r) => overlay(r.target) ||
      (r.type === "childList" && r.addedNodes.length > 0 && [...r.addedNodes].every(overlay));
    if (records && records.every(own)) return;
    window.__ultrademoLastMutation = performance.now();
  };
  mark();
  new MutationObserver(mark).observe(document, {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
})();"""
_SINCE_MUTATION_JS = (
    "() => window.__ultrademoLastMutation === undefined ? 1e9"
    " : performance.now() - window.__ultrademoLastMutation"
)


@dataclass
class SandboxConfig:
    session_id: str
    start_url: str
    allowed_domains: list[str]
    policy: dict[str, Any] = field(default_factory=dict)
    width: int = 1280
    height: int = 720
    locale: str = "en-US"
    action_timeout_ms: int = 8_000
    # True draws the cursor and highlights into the page itself, so they show up in the video.
    # The player draws them client-side from overlay events, so the agent turns this off.
    draw_overlays: bool = True


@dataclass
class ElementInfo:
    ref: str
    role: str
    name: str
    box: tuple[int, int, int, int] | None
    # Inside an iframe: its box is relative to that frame, not to the page.
    in_frame: bool = False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ref": self.ref, "role": self.role, "name": self.name}
        if self.box:
            d["bbox"] = {"x": self.box[0], "y": self.box[1], "w": self.box[2], "h": self.box[3]}
        return d


def host_allowed(url: str, allowed: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in ("about", "data", "blob"):
        return True
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in allowed))


def parse_elements(snapshot: str) -> dict[str, ElementInfo]:
    out: dict[str, ElementInfo] = {}
    # Indents of the iframe lines enclosing the current line. A frame's contents are nested
    # under its `- iframe` line, and their boxes come from that frame's own viewport. The ref
    # prefix can't tell: Playwright gives every frame but the first a prefix (`f<seq>`), and the
    # main frame gets one too once it has navigated.
    frames: list[int] = []
    for line in snapshot.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        indent = len(line) - len(line.lstrip())
        while frames and indent <= frames[-1]:
            frames.pop()
        box = tuple(int(v) for v in m["box"].split(",")) if m["box"] else None
        name = (m["name"] or "").replace('\\"', '"')
        out[m["ref"]] = ElementInfo(m["ref"], m["role"], name, box, bool(frames))  # type: ignore[arg-type]
        if m["role"] == "iframe":
            frames.append(indent)
    return out


def action_requests(
    spans: list[list[float | None]], acted: float, settle_from: float, quiet: float
) -> list[list[float | None]]:
    """The requests an action caused, out of all the page's recent ones.

    A request is the action's if it starts after the action began and before the page could have
    been called quiet: within `quiet` of the action finishing, of one of its requests answering,
    or while one of them is still in flight (a fetch chained on another). Background polling
    that happens to fire later is someone else's, so a page that polls still settles.
    """
    ours: list[list[float | None]] = []
    reach = settle_from + quiet
    for span in sorted(spans, key=lambda sp: sp[0]):  # type: ignore[arg-type,return-value]
        start, end = span
        assert start is not None
        if start < acted:
            continue
        if start > reach:
            break
        ours.append(span)
        reach = float("inf") if end is None else max(reach, end + quiet)
    return ours


class Sandbox:
    def __init__(self, config: SandboxConfig, browser: Browser, owns_browser: bool) -> None:
        self.config = config
        self.policy = Policy.from_agent(config.policy)
        self._browser = browser
        self._owns_browser = owns_browser
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._elements: dict[str, ElementInfo] = {}
        self._lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.started_at = time.monotonic()
        self.on_overlay: OverlaySink | None = None
        self._url_before = ""
        self._highlight_seq = 0
        # Recent data requests: when each started, and when it answered (None while in flight).
        self._requests: dict[Request, list[float | None]] = {}
        self._action_started = 0.0

    @classmethod
    async def launch(
        cls,
        pw: Playwright,
        config: SandboxConfig,
        executable_path: str | None = None,
        shared_browser: Browser | None = None,
    ) -> "Sandbox":
        browser = shared_browser or await pw.chromium.launch(
            executable_path=executable_path, args=["--disable-dev-shm-usage"]
        )
        sandbox = cls(config, browser, owns_browser=shared_browser is None)
        try:
            await sandbox._start()
        except BaseException:
            await sandbox.close()  # never leak a browser or context on a failed start
            raise
        return sandbox

    async def _start(self) -> None:
        c = self.config
        if not host_allowed(c.start_url, c.allowed_domains):
            raise ValueError("start_url is outside allowed_domains")
        self._context = await self._browser.new_context(
            viewport={"width": c.width, "height": c.height},
            locale=c.locale,
            device_scale_factor=1,
            accept_downloads=False,
        )
        self._context.set_default_timeout(c.action_timeout_ms)
        await self._context.add_init_script(_MUTATION_CLOCK_JS)
        if c.draw_overlays:
            await self._context.add_init_script(_OVERLAY_JS)
        await self._context.route("**/*", self._guard)
        self.page = await self._context.new_page()
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)
        # Registered after the main page exists, so only popups and new tabs reach the handler.
        self._context.on("page", self._on_new_page)
        await self.page.goto(c.start_url, wait_until="domcontentloaded")

    async def _guard(self, route: Route) -> None:
        req = route.request
        if req.is_navigation_request() and not host_allowed(req.url, self.config.allowed_domains):
            # A 204 answer makes the browser stay on the current page, where an abort would leave
            # the viewer looking at Chrome's error page.
            await route.fulfill(status=204, body="")
            return
        await route.fallback()

    async def _on_new_page(self, page: Page) -> None:
        if page is self.page:
            return
        url = page.url
        await page.close()
        if self.page and host_allowed(url, self.config.allowed_domains) and url != "about:blank":
            await self.page.goto(url, wait_until="domcontentloaded")

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._owns_browser:
                await self._browser.close()

    # --- observation ---------------------------------------------------------------------------

    async def snapshot(self) -> str:
        assert self.page is not None
        text = await self.page.locator("body").aria_snapshot(mode="ai", boxes=True)
        self._elements = parse_elements(text)
        if len(text) > MAX_SNAPSHOT_CHARS:
            text = text[:MAX_SNAPSHOT_CHARS] + "\n# [truncated: scroll or navigate to see the rest]"
        return text

    async def screenshot_b64(self) -> str:
        assert self.page is not None
        png = await self.page.screenshot(type="jpeg", quality=70)
        return base64.b64encode(png).decode()

    def _on_request(self, request: Request) -> None:
        if request.resource_type not in _DATA_REQUESTS:
            return
        now = time.monotonic()
        # Only the last action's requests matter, and a long poll may never answer.
        for r in [r for r, (t, _) in self._requests.items() if now - t > _REQUEST_FORGET_S]:
            del self._requests[r]
        self._requests[request] = [now, None]

    def _on_request_done(self, request: Request) -> None:
        if span := self._requests.get(request):
            span[1] = time.monotonic()

    async def _settle(self, quiet_ms: int = SETTLE_QUIET_MS) -> None:
        """Wait until the page has reacted to the action, then return as soon as it has.

        Not `networkidle`, which Playwright marks DISCOURAGED. After a page load it costs at least
        500 ms with no traffic at all, which a page with polling or analytics never reaches. After
        an in-page action it resolves at once, since the document already reached it, so it never
        waited for the data a click fetches. Instead, wait for what an action actually changes: the
        DOM, and the data requests the action started (see `action_requests`). The page is settled
        once neither has moved for `quiet_ms`.
        """
        assert self.page is not None
        page = self.page
        acted = self._action_started
        # Clicks already wait for a navigation they start to commit, so only its load is left.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except PlaywrightTimeout:
            return
        settle_from = time.monotonic()  # the action itself counts as the latest change
        deadline = settle_from + SETTLE_MAX_MS / 1000
        window = quiet = quiet_ms / 1000
        while True:
            now = time.monotonic()
            try:
                # Milliseconds since the DOM last changed, as the init script records it.
                since_dom = (await page.evaluate(_SINCE_MUTATION_JS)) / 1000
            except PlaywrightError:
                # The action navigated and this document is going away: wait for the next one.
                if now >= deadline:
                    return
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=3_000)
                except PlaywrightTimeout:
                    return
                settle_from = time.monotonic()
                continue
            ours = action_requests(list(self._requests.values()), acted, settle_from, window)
            busy = any(end is None for _, end in ours)
            last_done = max([settle_from, *(end for _, end in ours if end is not None)])
            if busy:
                last_done = now
            if ours:
                quiet = SETTLE_QUIET_MS / 1000  # a debounce, if any, has already fired
            waited = min(since_dom, now - last_done)
            if waited >= quiet:
                return
            if now >= deadline:
                log.info("settle_capped", url=page.url, busy=busy)
                return  # polling or animating pages never go quiet; the snapshot is still useful
            await asyncio.sleep(min(quiet - waited, _SETTLE_POLL_S, max(deadline - now, 0)))

    # --- actions -------------------------------------------------------------------------------

    async def execute(
        self, tool: str, raw_input: dict[str, Any], confirmed: bool = False
    ) -> ToolResult:
        if tool not in OPERATOR_TOOLS:
            return ToolResult(status="error", summary=f"{tool} is not an operator tool")
        try:
            args = parse_tool_input(tool, raw_input)
        except ValueError as e:
            return ToolResult(status="error", summary=f"Invalid input: {e}")
        async with self._lock:
            self.last_used = time.monotonic()
            t0 = time.perf_counter()
            try:
                result = await self._run(tool, args, confirmed)
            except PlaywrightTimeout:
                result = ToolResult(
                    status="error",
                    summary="The element did not respond in time. Observe the screen and try another way.",
                )
            except PlaywrightError as e:
                msg = str(e).splitlines()[0]
                result = ToolResult(status="error", summary=f"Browser error: {msg[:300]}")
            result.latency_ms = int((time.perf_counter() - t0) * 1000)
            return result

    def _element(self, ref: str) -> ElementInfo | None:
        return self._elements.get(ref)

    async def _overlay(self, type_: EventType, **payload: Any) -> None:
        if self.on_overlay is None:
            return
        payload.update(screen_w=self.config.width, screen_h=self.config.height)
        try:
            await self.on_overlay(type_, payload)
        except Exception as e:  # noqa: BLE001 - an overlay must never fail the action
            log.warning("overlay_publish_failed", error=str(e)[:200])

    async def _pointer_to(self, el: ElementInfo, loc: Locator, *, click: bool = False) -> None:
        """Glide the mouse to the element so the viewer sees where the agent is going.

        The box is measured now, after scrolling, in page coordinates: the snapshot's boxes can
        be stale once the page has scrolled, and are relative to their own frame.
        """
        assert self.page is not None
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
            box = await loc.bounding_box(timeout=2_000)
        except PlaywrightError:
            box = None
        if not box:
            return
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self._overlay(
            EventType.OVERLAY_CURSOR, x=round(cx), y=round(cy), kind="move", ref=el.ref
        )
        await self.page.mouse.move(cx, cy, steps=12)
        if click:
            await self._overlay(
                EventType.OVERLAY_CURSOR, x=round(cx), y=round(cy), kind="click", ref=el.ref
            )

    async def element_at(self, x: float, y: float) -> ElementInfo | None:
        """The element under a viewport point, as the agent would address it (a fresh ref).

        Used when the viewer points at the shared screen ("what's this?"). Hit-testing the
        snapshot's boxes, rather than `elementFromPoint`, returns the same role, name and ref the
        model sees in `operate_observe`, so it can act on the answer directly.
        """
        async with self._lock:
            self.last_used = time.monotonic()
            await self.snapshot()
            best: ElementInfo | None = None
            best_area = float("inf")
            for el in self._elements.values():
                # Boxes of elements inside iframes are relative to their frame.
                if el.box is None or el.role in _POINTABLE_SKIP or el.in_frame:
                    continue
                bx, by, bw, bh = el.box
                if bx <= x <= bx + bw and by <= y <= by + bh and bw * bh < best_area:
                    best, best_area = el, bw * bh
            return best

    async def _after_change(
        self,
        summary: str,
        el: ElementInfo | None = None,
        policy: PolicyClass = PolicyClass.ALLOWED,
        quiet_ms: int = SETTLE_QUIET_MS,
    ) -> ToolResult:
        assert self.page is not None
        await self._settle(quiet_ms)
        if self.page.url != self._url_before:
            # A new page: the old highlight and cursor no longer point at anything.
            await self._overlay(EventType.OVERLAY_CLEAR)
        return ToolResult(
            status="ok",
            summary=summary,
            url=self.page.url,
            title=await self.page.title(),
            snapshot=await self.snapshot(),
            element=el.as_dict() if el else None,
            policy=policy,
        )

    async def _gate(
        self, tool: str, el: ElementInfo, confirmed: bool, *, submits: bool = False
    ) -> ToolResult | None:
        cls = self.policy.classify(tool, el.role, el.name, submits=submits)
        if cls == PolicyClass.BLOCKED:
            return ToolResult(
                status="blocked",
                summary=f'"{el.name}" is not available in this demo.',
                element=el.as_dict(),
                policy=cls,
            )
        if cls == PolicyClass.CONFIRM and not confirmed:
            return ToolResult(
                status="needs_confirmation",
                summary=f'{tool.removeprefix("operate_").capitalize()} "{el.name}"',
                element=el.as_dict(),
                policy=cls,
            )
        return None

    async def _run(self, tool: str, args: Any, confirmed: bool) -> ToolResult:
        page = self.page
        self._url_before = page.url if page else ""
        self._action_started = time.monotonic()
        assert page is not None

        if tool == "operate_observe":
            return ToolResult(
                status="ok",
                summary="Observed the screen",
                url=page.url,
                title=await page.title(),
                snapshot=await self.snapshot(),
                screenshot_jpeg_b64=await self.screenshot_b64() if args.screenshot else None,
            )

        if tool == "operate_navigate":
            target = urljoin(page.url, args.url)
            if not host_allowed(target, self.config.allowed_domains):
                return ToolResult(
                    status="blocked",
                    summary="That address is outside this product.",
                    policy=PolicyClass.BLOCKED,
                )
            await self._overlay(EventType.OVERLAY_CLEAR)
            await page.goto(target, wait_until="domcontentloaded")
            return await self._after_change(f"Opened {urlparse(target).path or '/'}")

        if tool == "operate_press":
            focused = await page.evaluate(
                "() => { const a = document.activeElement; if (!a) return ['', ''];"
                " return [a.getAttribute('role') || a.tagName.toLowerCase(),"
                " (a.getAttribute('aria-label') || a.innerText || a.value || '').slice(0, 200)]; }"
            )
            role, name = focused
            el = ElementInfo("focused", "button" if role in ("button", "a") else role, name, None)
            submits = role in ("button", "a") and args.keys == "Enter"
            if gate := await self._gate(tool, el, confirmed, submits=submits):
                return gate
            await page.keyboard.press(args.keys)
            return await self._after_change(f"Pressed {args.keys}")

        if tool == "operate_scroll" and args.ref is None:
            await self._overlay(EventType.OVERLAY_CLEAR)
            await page.mouse.wheel(0, args.dy)
            await asyncio.sleep(0.15)
            return ToolResult(
                status="ok",
                summary=f"Scrolled {'down' if args.dy > 0 else 'up'}",
                url=page.url,
                snapshot=await self.snapshot(),
            )

        el = self._element(args.ref)
        if el is None:
            return ToolResult(
                status="error",
                summary=f"Unknown ref {args.ref}. Call operate_observe for fresh refs.",
            )
        loc = page.locator(f"aria-ref={args.ref}")

        if tool == "operate_highlight":
            await loc.scroll_into_view_if_needed()
            box = await loc.bounding_box()
            if box and self.config.draw_overlays:
                await page.evaluate(
                    "b => window.__ultrademo && window.__ultrademo.highlight(b)",
                    {**box, "label": args.label or ""},
                )
            if box:
                self._highlight_seq += 1
                await self._overlay(
                    EventType.OVERLAY_HIGHLIGHT,
                    id=f"hl_{self._highlight_seq}",
                    bbox={
                        "x": round(box["x"]),
                        "y": round(box["y"]),
                        "w": round(box["width"]),
                        "h": round(box["height"]),
                    },
                    label=args.label or "",
                    ttl_ms=HIGHLIGHT_TTL_MS,
                )
            return ToolResult(status="ok", summary=f'Highlighted "{el.name}"', element=el.as_dict())

        if tool == "operate_hover":
            await self._pointer_to(el, loc)
            await loc.hover()
            return await self._after_change(f'Hovered "{el.name}"', el)

        if tool == "operate_scroll":
            await self._overlay(EventType.OVERLAY_CLEAR)
            await loc.evaluate("(n, dy) => n.scrollBy({top: dy, behavior: 'instant'})", args.dy)
            return ToolResult(
                status="ok", summary="Scrolled", url=page.url, snapshot=await self.snapshot()
            )

        if tool == "operate_click":
            if gate := await self._gate(tool, el, confirmed):
                return gate
            await self._pointer_to(el, loc, click=True)
            await loc.click()
            return await self._after_change(
                f'Clicked "{el.name}"', el, self.policy.classify(tool, el.role, el.name)
            )

        if tool == "operate_type":
            if gate := await self._gate(tool, el, confirmed, submits=args.submit):
                return gate
            await self._pointer_to(el, loc)
            await loc.fill(args.text)
            if args.submit:
                await loc.press("Enter")
            # Search-as-you-type fields usually debounce before they fetch.
            quiet = SETTLE_QUIET_MS if args.submit else SETTLE_TYPING_QUIET_MS
            return await self._after_change(f'Typed into "{el.name}"', el, quiet_ms=quiet)

        if tool == "operate_select":
            await self._pointer_to(el, loc)
            if el.role == "combobox":
                try:
                    await loc.select_option(label=args.value)
                except PlaywrightError:
                    await loc.click()
                    await page.get_by_role("option", name=args.value, exact=False).first.click()
            else:
                await loc.select_option(label=args.value)
            return await self._after_change(f'Chose "{args.value}" in "{el.name}"', el)

        return ToolResult(status="error", summary=f"{tool} is not implemented")

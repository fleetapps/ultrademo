import { describe, expect, it, vi } from "vitest";

import { buildCsp, livekitOrigins, safeFrameAncestors } from "@/lib/csp";
import { isSafeUrl, resolveCta } from "@/lib/ctas";
import { hostTargets, postToHost } from "@/lib/embed";
import { launchParams } from "@/lib/launch";
import { rectPercent, stagePointToViewport } from "@/lib/overlay";
import { parseAgentState, parseConfirmRequest, parseEvent, parseScreenMeta } from "@/lib/protocol";
import { accentVars, contrast } from "@/lib/theme";
import { MAX_LINES, upsertLine } from "@/lib/transcript";

const enc = (o: unknown) => new TextEncoder().encode(JSON.stringify(o));

describe("protocol", () => {
  it("parses and bounds server events", () => {
    expect(
      parseEvent(enc({ v: 1, seq: 3, type: "overlay.highlight", bbox: { x: 1, y: 2, w: 3, h: 4 }, ttl_ms: 999999 })),
    ).toMatchObject({
      type: "overlay.highlight",
      ttlMs: 30000,
      screen: { w: 1280, h: 720 },
    });
    expect(parseEvent(enc({ v: 2, type: "cta.show" }))).toBeNull();
    expect(parseEvent(enc({ v: 1, type: "unknown" }))).toBeNull();
    expect(parseEvent(enc({ v: 1, type: "overlay.cursor", x: "1", y: 2 }))).toBeNull();
    expect(parseEvent(new TextEncoder().encode("not json"))).toBeNull();
    expect(
      parseEvent(enc({ v: 1, type: "action.completed", tool: "operate_click", element: { ref: "e1" } })),
    ).toMatchObject({ element: null });
  });

  it("parses attributes and RPC payloads defensively", () => {
    expect(parseAgentState("acting")).toBe("acting");
    expect(parseAgentState("dancing")).toBeNull();
    expect(parseScreenMeta('{"viewport":{"w":1920,"h":1080}}')).toEqual({ w: 1920, h: 1080 });
    expect(parseScreenMeta('{"viewport":{"w":-1,"h":1080}}')).toBeNull();
    expect(parseScreenMeta("nope")).toBeNull();
    expect(parseConfirmRequest('{"summary":"Save"}')).toEqual({ summary: "Save", element: null });
    expect(parseConfirmRequest('{"summary":""}')).toBeNull();
  });
});

describe("transcript", () => {
  it("replaces interim text by segment and never un-finalizes", () => {
    let lines = upsertLine([], { id: "u1", speaker: "viewer", text: "sho", final: false, at: 1 });
    lines = upsertLine(lines, { id: "u1", speaker: "viewer", text: "show me", final: true, at: 1 });
    lines = upsertLine(lines, { id: "u1", speaker: "viewer", text: "", final: false, at: 1 });
    expect(lines).toEqual([{ id: "u1", speaker: "viewer", text: "show me", final: true, source: "voice", at: 1 }]);
    expect(upsertLine([], { id: "x", speaker: "agent", text: "  ", final: false, at: 1 })).toEqual([]);
  });

  it("keeps a bounded history", () => {
    let lines = upsertLine([], { id: "0", speaker: "agent", text: "a", final: true, at: 0 });
    for (let i = 1; i <= MAX_LINES + 5; i++)
      lines = upsertLine(lines, { id: String(i), speaker: "agent", text: "a", final: true, at: i });
    expect(lines).toHaveLength(MAX_LINES);
    expect(lines[0]?.id).toBe("6");
  });
});

describe("overlay geometry", () => {
  const vp = { w: 1280, h: 720 };
  it("maps boxes to percentages and clamps them to the screen", () => {
    expect(rectPercent({ x: 640, y: 360, w: 128, h: 72 }, vp)).toEqual({ left: 50, top: 50, width: 10, height: 10 });
    expect(rectPercent({ x: 1200, y: -10, w: 400, h: 100 }, vp).width).toBeCloseTo(6.25);
  });
  it("maps clicks back to viewport pixels", () => {
    const rect = { left: 100, top: 50, width: 640, height: 360 };
    expect(stagePointToViewport(420, 230, rect, vp)).toEqual({ x: 640, y: 360 });
    expect(stagePointToViewport(99, 230, rect, vp)).toBeNull();
  });
});

describe("csp", () => {
  it("allows the LiveKit host and the nonce, and denies framing by default", () => {
    const csp = buildCsp({ nonce: "abc", livekitUrl: "wss://demo-xyz.livekit.cloud", dev: false, frameAncestors: [] });
    expect(csp).toContain("script-src 'self' 'nonce-abc' 'strict-dynamic'");
    expect(csp).toContain("wss://demo-xyz.livekit.cloud https://demo-xyz.livekit.cloud https://*.livekit.cloud");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("upgrade-insecure-requests");
  });
  it("only accepts well-formed https origins for frame-ancestors", () => {
    expect(
      safeFrameAncestors([
        "https://www.acme.com",
        "http://x.com",
        "https://a.com; script-src *",
        "*",
        "http://localhost:3999",
        "http://localhost.evil.com",
      ]),
    ).toEqual(["https://www.acme.com", "http://localhost:3999"]);
    expect(livekitOrigins("ws://localhost:7880")).toEqual(["ws://localhost:7880", "http://localhost:7880"]);
    expect(livekitOrigins("::")).toEqual([]);
  });
});

describe("embed bridge", () => {
  it("posts once per allowed origin, or to * without an allowlist", () => {
    const target = { postMessage: vi.fn() };
    expect(postToHost("cta.clicked", { cta_id: "book-0" }, ["https://a.com", "https://b.com"], target)).toBe(2);
    expect(target.postMessage).toHaveBeenCalledWith(
      { source: "ultrademo", v: 1, type: "cta.clicked", detail: { cta_id: "book-0" } },
      "https://a.com",
    );
    expect(hostTargets([])).toEqual(["*"]);
    // A malformed entry is dropped rather than making postMessage throw.
    expect(hostTargets(["https://a.com", "acme.com"])).toEqual(["https://a.com"]);
    const throwing = {
      postMessage: () => {
        throw new SyntaxError("bad origin");
      },
    };
    expect(postToHost("ready", {}, ["https://a.com"], throwing)).toBe(0);
    expect(postToHost("ready", {}, [], null)).toBe(0);
  });
});

describe("launch params, CTAs and theme", () => {
  it("keeps only attribution params and a well-formed token", () => {
    expect(launchParams({ t: "abcDEF123_-", test: "1", utm_source: "email", evil: "x", ref: ["a", "b"] })).toEqual({
      token: "abcDEF123_-",
      test: true,
      params: { utm_source: "email", ref: "a" },
    });
    expect(launchParams({ t: "<script>" }).token).toBeNull();
  });
  it("resolves CTAs by kind and only follows https links", () => {
    const ctas = [{ id: "b", kind: "book", label: "Book" }];
    expect(resolveCta(ctas, "book")?.id).toBe("b");
    expect(resolveCta(ctas, "link")).toBeUndefined();
    expect(isSafeUrl("https://cal.com/x")).toBe(true);
    expect(isSafeUrl("javascript:alert(1)")).toBe(false);
  });
  it("picks readable text on the brand colour", () => {
    expect(accentVars("#ffe600")["--on-accent"]).toBe("#000000");
    // Every grey, including the mid greys where #111 fell short, reaches AA with one of the two.
    for (let g = 0; g < 256; g += 5) {
      const hex = `#${g.toString(16).padStart(2, "0").repeat(3)}`;
      const vars = accentVars(hex);
      expect(contrast(vars["--accent"] ?? "", vars["--on-accent"] ?? "")).toBeGreaterThanOrEqual(4.5);
    }
    expect(accentVars("#0f7f82")["--on-accent"]).toBe("#ffffff");
    expect(accentVars("red")["--accent"]).toBe("#0f7f82");
    const v = accentVars("#0f7f82");
    expect(contrast(v["--accent"] ?? "", v["--on-accent"] ?? "")).toBeGreaterThanOrEqual(4.5);
  });
});

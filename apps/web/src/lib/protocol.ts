// Wire names and payload shapes of the realtime session protocol (docs/04 §1).
// Mirrors packages/protocol/src/ultrademo_protocol/envelope.py; change both together.

export const TOPIC_EVENTS = "ultrademo.events";
// LiveKit Agents' own text-stream topics: the agent publishes agent and viewer transcripts on
// `lk.transcription` and reads typed chat from `lk.chat` (livekit-agents 1.8, voice/room_io).
export const TOPIC_TRANSCRIPTION = "lk.transcription";
export const TOPIC_CHAT = "lk.chat";
export const ATTR_SEGMENT_ID = "lk.segment_id";
export const ATTR_TRANSCRIPTION_FINAL = "lk.transcription_final";

export const ATTR_AGENT_STATE = "ultrademo.agent_state";
export const ATTR_SCREEN_META = "ultrademo.screen_meta";
export const ATTR_KIND = "ultrademo.kind";
export const RPC_CONFIRM = "ultrademo.confirm";
export const RPC_POINTER = "ultrademo.pointer";

export const AGENT_STATES = ["idle", "listening", "thinking", "speaking", "acting", "handoff", "ended"] as const;
export type AgentState = (typeof AGENT_STATES)[number];

export function parseAgentState(value: string | undefined): AgentState | null {
  return (AGENT_STATES as readonly string[]).includes(value ?? "") ? (value as AgentState) : null;
}

export interface BBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ElementRef {
  ref: string;
  role: string;
  name: string;
  bbox?: BBox;
}

export interface Viewport {
  w: number;
  h: number;
}

export const DEFAULT_VIEWPORT: Viewport = { w: 1280, h: 720 };

/** `ultrademo.screen_meta` on the sandbox participant: `{"viewport": {"w", "h"}}`. */
export function parseScreenMeta(value: string | undefined): Viewport | null {
  if (!value) return null;
  try {
    const v = (JSON.parse(value) as { viewport?: unknown }).viewport as Partial<Viewport> | undefined;
    if (v && isPositive(v.w) && isPositive(v.h)) return { w: v.w, h: v.h };
  } catch {
    // fall through
  }
  return null;
}

function isPositive(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n) && n > 0 && n <= 10_000;
}

function isNumber(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

function str(v: unknown, max = 500): string {
  return typeof v === "string" ? v.slice(0, max) : "";
}

function bbox(v: unknown): BBox | undefined {
  if (!v || typeof v !== "object") return undefined;
  const b = v as Record<string, unknown>;
  if (isNumber(b.x) && isNumber(b.y) && isNumber(b.w) && isNumber(b.h) && b.w >= 0 && b.h >= 0) {
    return { x: b.x, y: b.y, w: b.w, h: b.h };
  }
  return undefined;
}

export function parseElement(v: unknown): ElementRef | null {
  if (!v || typeof v !== "object") return null;
  const e = v as Record<string, unknown>;
  if (typeof e.ref !== "string" || typeof e.role !== "string") return null;
  return { ref: e.ref, role: e.role, name: str(e.name, 200), bbox: bbox(e.bbox) };
}

// --- events on TOPIC_EVENTS -------------------------------------------------------------------

export type ServerEvent =
  | { type: "action.started"; seq: number; tool: string; args: Record<string, unknown> }
  | {
      type: "action.completed" | "action.failed";
      seq: number;
      tool: string;
      status: string;
      label: string;
      element: ElementRef | null;
    }
  | {
      type: "overlay.cursor";
      seq: number;
      x: number;
      y: number;
      kind: "move" | "click";
      screen: Viewport;
    }
  | {
      type: "overlay.highlight";
      seq: number;
      id: string;
      bbox: BBox;
      label: string;
      ttlMs: number;
      screen: Viewport;
    }
  | { type: "overlay.clear"; seq: number }
  | { type: "cta.show"; seq: number; kind: string; label: string }
  | { type: "session.status"; seq: number; status: string; reason: string };

function screenOf(e: Record<string, unknown>): Viewport {
  return isPositive(e.screen_w) && isPositive(e.screen_h) ? { w: e.screen_w, h: e.screen_h } : DEFAULT_VIEWPORT;
}

/**
 * Decode and validate one data packet. Anything malformed or unknown returns null: the player
 * ignores what it does not understand rather than failing the call.
 */
export function parseEvent(payload: Uint8Array): ServerEvent | null {
  let e: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(new TextDecoder().decode(payload));
    if (!parsed || typeof parsed !== "object") return null;
    e = parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  if (e.v !== 1 || typeof e.type !== "string") return null;
  const seq = isNumber(e.seq) ? e.seq : 0;
  switch (e.type) {
    case "action.started":
      return {
        type: e.type,
        seq,
        tool: str(e.tool, 64),
        args: e.args && typeof e.args === "object" ? (e.args as Record<string, unknown>) : {},
      };
    case "action.completed":
    case "action.failed":
      return {
        type: e.type,
        seq,
        tool: str(e.tool, 64),
        status: str(e.status, 32),
        label: str(e.label, 300),
        element: parseElement(e.element),
      };
    case "overlay.cursor":
      if (!isNumber(e.x) || !isNumber(e.y)) return null;
      return {
        type: e.type,
        seq,
        x: e.x,
        y: e.y,
        kind: e.kind === "click" ? "click" : "move",
        screen: screenOf(e),
      };
    case "overlay.highlight": {
      const b = bbox(e.bbox);
      if (!b) return null;
      return {
        type: e.type,
        seq,
        id: str(e.id, 64) || `hl_${seq}`,
        bbox: b,
        label: str(e.label, 120),
        ttlMs: isNumber(e.ttl_ms) ? Math.min(Math.max(e.ttl_ms, 500), 30_000) : 4_000,
        screen: screenOf(e),
      };
    }
    case "overlay.clear":
      return { type: e.type, seq };
    case "cta.show":
      return { type: e.type, seq, kind: str(e.kind, 32), label: str(e.label, 80) };
    case "session.status":
      return { type: e.type, seq, status: str(e.status, 32), reason: str(e.reason, 200) };
    default:
      return null;
  }
}

/** Parse the agent's `ultrademo.confirm` RPC payload. */
export function parseConfirmRequest(payload: string): { summary: string; element: ElementRef | null } | null {
  try {
    const p = JSON.parse(payload) as Record<string, unknown>;
    const summary = str(p.summary, 300);
    if (!summary) return null;
    return { summary, element: parseElement(p.element) };
  } catch {
    return null;
  }
}

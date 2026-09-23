// The player side of the embed bridge (docs/04 §5). Events go to the host page with postMessage.
// With an origin allowlist, a message is posted once per allowed origin: the browser delivers it
// only when the target origin matches the actual parent, so no other page ever receives it.

import { safeFrameAncestors } from "./csp";

export type HostEvent = "ready" | "session.started" | "session.ended" | "cta.clicked" | "handoff.requested";

export interface HostMessage {
  source: "ultrademo";
  v: 1;
  type: HostEvent;
  detail: Record<string, string | number | boolean | null>;
}

/**
 * The same origins `frame-ancestors` allows (proxy.ts): an empty list means any host, and an
 * entry that isn't a well-formed origin is dropped, since postMessage would throw on it.
 */
export function hostTargets(allowedOrigins: readonly string[]): string[] {
  return allowedOrigins.length ? safeFrameAncestors(allowedOrigins) : ["*"];
}

export function postToHost(
  type: HostEvent,
  detail: HostMessage["detail"],
  allowedOrigins: readonly string[],
  target: Pick<Window, "postMessage"> | null = typeof window !== "undefined" && window.parent !== window
    ? window.parent
    : null,
): number {
  if (!target) return 0;
  const message: HostMessage = { source: "ultrademo", v: 1, type, detail };
  const targets = hostTargets(allowedOrigins);
  let sent = 0;
  for (const origin of targets) {
    try {
      target.postMessage(message, origin);
      sent++;
    } catch {
      // A host bridge must never break the call.
    }
  }
  return sent;
}

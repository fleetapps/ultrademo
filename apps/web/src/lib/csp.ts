// Content Security Policy for the player (docs/06 §4): scripts only with the per-request nonce,
// network only to ourselves and the LiveKit server, and framing only on the embed route.

export interface CspOptions {
  nonce: string;
  livekitUrl: string;
  dev: boolean;
  /** `'none'` for regular pages; the launch config's allowed origins (or `*`) for embeds. */
  frameAncestors: readonly string[];
}

/** `wss://x.livekit.cloud` also needs `https://x.livekit.cloud` (region discovery, validation). */
export function livekitOrigins(url: string): string[] {
  try {
    const u = new URL(url);
    const secure = u.protocol === "wss:" || u.protocol === "https:";
    const ws = `${secure ? "wss" : "ws"}://${u.host}`;
    const http = `${secure ? "https" : "http"}://${u.host}`;
    const out = [ws, http];
    // LiveKit Cloud routes a connection to region-specific hosts under livekit.cloud.
    if (u.hostname.endsWith(".livekit.cloud")) out.push("https://*.livekit.cloud", "wss://*.livekit.cloud");
    return out;
  } catch {
    return [];
  }
}

export function buildCsp(o: CspOptions): string {
  const directives: [string, string[]][] = [
    ["default-src", ["'self'"]],
    ["script-src", ["'self'", `'nonce-${o.nonce}'`, "'strict-dynamic'", ...(o.dev ? ["'unsafe-eval'"] : [])]],
    // Next.js puts the nonce on the styles it inlines. React renders `style` attributes, which a
    // nonce cannot cover, so attributes (not elements) may be inline.
    ["style-src", ["'self'", `'nonce-${o.nonce}'`]],
    ["style-src-attr", ["'unsafe-inline'"]],
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    ["font-src", ["'self'"]],
    ["media-src", ["'self'", "blob:", "mediastream:"]],
    ["connect-src", ["'self'", ...livekitOrigins(o.livekitUrl), ...(o.dev ? ["ws:"] : [])]],
    ["worker-src", ["'self'", "blob:"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["form-action", ["'self'"]],
    ["frame-ancestors", o.frameAncestors.length ? [...o.frameAncestors] : ["'none'"]],
  ];
  const csp = directives.map(([k, v]) => `${k} ${v.join(" ")}`).join("; ");
  return o.dev ? csp : `${csp}; upgrade-insecure-requests`;
}

const ORIGIN_RE = /^https:\/\/[a-z0-9.-]+(:\d{1,5})?$/i;
const LOCAL_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/i;

/**
 * Only well-formed https origins (and plain-http localhost, for development) may be put into a
 * header; anything else is dropped.
 */
export function safeFrameAncestors(origins: readonly string[]): string[] {
  return origins.filter((o) => ORIGIN_RE.test(o) || LOCAL_RE.test(o));
}

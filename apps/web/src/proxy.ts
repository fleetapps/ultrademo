import { type NextRequest, NextResponse } from "next/server";

import { buildCsp, safeFrameAncestors } from "@/lib/csp";

// Security headers for every page (docs/06 §4): a nonce-based CSP, and framing denied everywhere
// except /d/<slug>/embed, where frame-ancestors is the launch config's embed allowlist.
// Next.js reads the nonce from the request's CSP header and applies it to its own scripts
// (node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md).

const EMBED_RE = /^\/d\/([a-z0-9][a-z0-9-]{1,62})\/embed\/?$/;

async function embedOrigins(slug: string, test: boolean): Promise<string[] | null> {
  const api = (process.env.ULTRADEMO_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
  try {
    const res = await fetch(`${api}/v1/public/launch-configs/${slug}${test ? "?test=true" : ""}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2_000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { embed_origins?: unknown };
    return Array.isArray(body.embed_origins)
      ? body.embed_origins.filter((o): o is string => typeof o === "string")
      : [];
  } catch {
    return null;
  }
}

export async function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const dev = process.env.NODE_ENV === "development";

  let frameAncestors: string[] = [];
  const embed = EMBED_RE.exec(request.nextUrl.pathname);
  if (embed) {
    const test = ["1", "true"].includes(request.nextUrl.searchParams.get("test") ?? "");
    const origins = await embedOrigins(embed[1] ?? "", test);
    // No allowlist means any site may embed the demo (it is public anyway). If the lookup fails,
    // framing stays denied: the page could not start a session without the API either.
    frameAncestors = origins === null ? [] : origins.length ? safeFrameAncestors(origins) : ["*"];
  }

  const csp = buildCsp({
    nonce,
    livekitUrl: process.env.ULTRADEMO_LIVEKIT_URL ?? "ws://localhost:7880",
    dev,
    frameAncestors,
  });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Permissions-Policy", "microphone=(self), camera=(), geolocation=(), payment=()");
  if (!embed) response.headers.set("X-Frame-Options", "DENY");
  if (!dev) response.headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|healthz|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};

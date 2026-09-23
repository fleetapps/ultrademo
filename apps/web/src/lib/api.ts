import "server-only";

import type { PublicLaunchConfig } from "./player-types";

// Server-side reads from the ultrademo API. The browser never talks to the API directly: the few
// public calls it makes go through this app's route handlers (src/app/api), so the API origin,
// CORS and client IPs stay under our control.

export type LaunchConfig = PublicLaunchConfig;

export function apiUrl(): string {
  return (process.env.ULTRADEMO_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
}

export function livekitUrl(): string {
  return process.env.ULTRADEMO_LIVEKIT_URL ?? "ws://localhost:7880";
}

const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;

export function isSlug(value: string): boolean {
  return SLUG_RE.test(value);
}

/** The public part of a launch config, or null when the demo does not exist or is not live. */
export async function fetchLaunchConfig(slug: string, test = false): Promise<LaunchConfig | null> {
  if (!isSlug(slug)) return null;
  const url = `${apiUrl()}/v1/public/launch-configs/${slug}${test ? "?test=true" : ""}`;
  const res = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(5_000) });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`launch config lookup failed: ${res.status}`);
  return (await res.json()) as LaunchConfig;
}

import type { LaunchParams } from "./player-types";

// Launch parameters a demo link may carry: `t` (context-link token), `test`, and marketing
// attribution. Everything else in the query string is ignored.

const PARAM_RE = /^(utm_[a-z]{1,20}|ref|source|campaign)$/;
const MAX_PARAMS = 10;

type Search = Record<string, string | string[] | undefined>;

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

export function launchParams(search: Search): LaunchParams {
  const token = first(search.t);
  const test = first(search.test);
  const params: Record<string, string> = {};
  for (const [k, v] of Object.entries(search)) {
    const value = first(v);
    if (!PARAM_RE.test(k) || !value || Object.keys(params).length >= MAX_PARAMS) continue;
    params[k] = value.slice(0, 200);
  }
  return {
    token: token && /^[A-Za-z0-9_-]{8,64}$/.test(token) ? token : null,
    test: test === "1" || test === "true",
    params,
  };
}

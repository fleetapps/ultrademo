import "server-only";

import { apiUrl } from "./api";

// Route handlers forward a few public calls to the API. Bodies are small; anything larger is
// refused before it reaches the API.

const MAX_BODY_BYTES = 16 * 1024;

/** The body as text, or null once it passes the cap. Reads the stream, so a huge body is never buffered. */
async function readCapped(request: Request): Promise<string | null> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  return new TextDecoder().decode(Buffer.concat(chunks));
}

export async function forward(request: Request, path: string, headers: Record<string, string> = {}): Promise<Response> {
  const body = await readCapped(request);
  if (body === null) {
    return Response.json({ error: "too_large", detail: "Request body too large" }, { status: 413 });
  }
  // X-Forwarded-For is not passed on: the viewer can set it, and only a trusted edge in front of
  // the player may. The api does not rely on client addresses.
  const origin = request.headers.get("origin");
  try {
    const res = await fetch(`${apiUrl()}${path}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(origin ? { origin } : {}),
        ...headers,
      },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return new Response(res.status === 204 ? null : await res.text(), {
      status: res.status,
      headers: res.status === 204 ? {} : { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ error: "unavailable", detail: "The demo service is unavailable" }, { status: 502 });
  }
}

import type { NextRequest } from "next/server";

import { forward } from "@/lib/forward";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** CTA clicks and feedback, authorized by the session receipt the start response carried. */
export async function POST(request: NextRequest, ctx: RouteContext<"/api/sessions/[id]/events">) {
  const { id } = await ctx.params;
  if (!UUID_RE.test(id)) return Response.json({ error: "not_found", detail: "Session not found" }, { status: 404 });
  const auth = request.headers.get("authorization");
  return forward(request, `/v1/public/sessions/${id}/events`, auth ? { authorization: auth } : {});
}

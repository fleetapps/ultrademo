import { forward } from "@/lib/forward";

/** Start a demo session (the API mints the LiveKit token and dispatches the agent). */
export async function POST(request: Request) {
  return forward(request, "/v1/public/sessions");
}

# ADR 15: Decisions made while building the player slice (2026-09-23)

Status: accepted for the player slice (`apps/web`). Revisit each one at the point noted.

## 1. The player draws overlays (supersedes ADR 14 §3)
The operator now publishes `overlay.cursor`, `overlay.highlight` and `overlay.clear` on `ultrademo.events` from the sandbox participant. It stamps each event with the screen size, and the agent starts sandboxes with `draw_overlays=false`. The stage keeps the sandbox's aspect ratio, so the player places overlays as plain percentages. They stay sharp at any size and cost no video bits.
The player accepts overlays only from the participant whose `ultrademo.kind` attribute is `sandbox`, and every other event only from the agent (`ParticipantKind.AGENT`). The sandbox token cannot update its own metadata or attributes.

## 2. Transcripts and chat use LiveKit Agents' own text streams
The agent already publishes agent and viewer transcripts on `lk.transcription`, with `lk.segment_id` and `lk.transcription_final`, and reads typed chat from `lk.chat`. The player uses those two topics rather than the `ultrademo.transcript` topic and the `chat.message` event of docs/04 §1, so a voice turn and a typed turn follow the same path. `TOPIC_TRANSCRIPT` was removed from the protocol, and the two attribute names are in `ultrademo_protocol` (`ATTR_SEGMENT_ID`, `ATTR_TRANSCRIPTION_FINAL`) and `apps/web/src/lib/protocol.ts`.
Streams travel on the data channel, while participant announcements travel on the signal connection. So the agent's first words can arrive before the player knows the agent has joined. The player waits up to 3 s for the announcement instead of dropping them (the browser test found this).

## 3. The browser talks to the api only through the player's own route handlers
`POST /api/sessions` and `POST /api/sessions/{id}/events` forward to the api with a 16 KB body cap (counted while streaming, not trusted from `Content-Length`) and a 10 s timeout. They pass `Origin` through, so the api's embed-origin check still sees the real page. They do not forward `X-Forwarded-For`: the player cannot tell a real client address from one the caller made up, and nothing in the api uses it yet. The api URL is server-side configuration (`ULTRADEMO_API_URL`), so the api needs no CORS. In production it should not be reachable from the internet. docker-compose publishes port 8000 only so developers can call it.
Revisit if the public endpoints move behind a CDN edge function.

## 4. Session receipts authorize viewer events
The start response carries a receipt: an HMAC-SHA256 of the session id under `ULTRADEMO_RECEIPT_SECRET`. The player sends it as a Bearer token to report CTA clicks and feedback. The rules, all enforced under a row lock on the session so concurrent requests can't get past them:
- A session that the agent has not marked started answers `409 not_started`. The receipt expires 24 h after `started_at` (`410`).
- A CTA click must name a CTA of the launch config (`422 unknown_cta`). The stored row takes its kind, label and URL from the launch config, never from the request. At most 50 clicks per session (`429`).
- Only the latest feedback is kept. Sending the same answer again writes nothing, and a session may change its answer at most 5 times (`429`). Every event also writes to the outbox (`cta.clicked`, `session.feedback`), so a later slice can turn these rows into webhooks and CRM updates.
Revisit when viewers can sign in (they would use the session instead).

## 5. CTAs come only from the launch config
`cta.show` carries a CTA kind, and the player looks up the button and URL in the launch config by that kind. The model can promote a button but can never inject a link. Only `https:` URLs are rendered as links.

## 6. Headers are set per request in `proxy.ts`
- The CSP uses a nonce with `strict-dynamic`. A nonce only works on pages rendered per request, so every page is dynamic: the player pages read `searchParams`, and the pages that don't (such as `not-found`) call `await connection()`. The player needs this anyway for per-environment config.
- `connect-src` allows the LiveKit URL from `ULTRADEMO_LIVEKIT_URL`.
- `/d/[slug]/embed` sets `frame-ancestors` from the launch config's `embed_origins`. Only https origins are allowed, plus plain-http localhost for development. An empty list lets any site embed the demo, since it is public anyway. If the lookup fails, framing is denied.
- Every other page sends `frame-ancestors 'none'` and `X-Frame-Options: DENY`.
- `Permissions-Policy` allows the microphone only for the page itself.
The embed page posts `ready`, `session.started`, `cta.clicked`, `handoff.requested` and `session.ended` to the host page, once per allowed origin.

## 7. Pointing is answered from the model's own snapshot
The viewer clicks the stage. The player calls the `ultrademo.pointer` RPC on the agent with viewport pixels, and the operator hit-tests a fresh accessibility snapshot (`/element-at`). The answer is the same role, name and ref the model acts on, so it can say "that's the Deals link" and click it. A click also starts a turn, as if the viewer had asked aloud. Calls from anyone but the viewer are refused, and calls are throttled to one every 0.75 s.

## 8. Biome instead of ESLint
The player uses TypeScript 7, whose native compiler has no JS API yet. `typescript-eslint`, and with it `eslint-config-next`, refuses to run on it. Biome 2.5 lints and formats without the TS API, and its `react` and `next` domains cover the rules we would have used.
Revisit when typescript-eslint supports TS 7.

## 9. The browser test runs everything but the voice pipeline and Claude
`apps/web/e2e` starts a real LiveKit server, the api on a fresh database, the operator with the sample product, and the standalone Next server. In place of the voice agent it runs `e2e/scripted_agent.py`, on the real LiveKit Agents worker. The script is dispatched by name from the viewer's token and uses the real `RoomHooks`, `Pointer` and api and operator clients. It speaks on the same text streams LiveKit Agents uses, so it needs no API keys and runs in CI.
The test covers joining, the greeting and the viewer's words, video frames, the cursor and highlight, the confirm dialog, pointing, a promoted CTA and its recorded click, the agent ending the call, and feedback. Afterwards it checks the stored rows.
It does not cover STT, TTS, turn detection or the Claude brain. Those need keys and belong to the eval harness (next slices).

## 10. Found by the browser test and fixed
- `OperatorClient.start` posted to `/v1/sandboxes/`, because httpx gives a base URL a trailing slash. FastAPI answered 307, so no sandbox would have started. The client now uses absolute paths, and a unit test pins them.
- LiveKit Agents sends all worker traffic through `HTTPS_PROXY` when it is set, and ignores `NO_PROXY`. The scripted agent passes `http_proxy=None`. A production worker behind an egress proxy must set `AgentServer(http_proxy=...)` deliberately.
- The first-words race in §2.
- Playwright gives the main frame a ref prefix (`f1e3`) once it has navigated, so a prefix does not mean "inside an iframe". Pointing now tells iframe contents apart by their nesting under an `iframe` node in the snapshot.

## 11. Verified against installed sources and docs (not assumed)
- Next.js 16.3.6, from its bundled docs in `node_modules/next/dist/docs`:
  - `proxy.ts` (it replaced middleware) and the nonce CSP recipe.
  - `PageProps` and `RouteContext` from `next typegen`.
  - `output: "standalone"`, and the official `with-docker` Dockerfile.
- React 19.3.
- livekit-client 2.22.3:
  - `room.registerTextStreamHandler` and `room.registerRpcMethod`. The `localParticipant.registerRpcMethod` form is deprecated.
  - `performRpc` timeouts are in ms and are clamped to at least 8 s.
  - App RPC error codes must fall outside 1001–1999.
- @livekit/components-react 2.9.24: only `RoomAudioRenderer`. Its session and transcription hooks are still `@beta`.
- LiveKit Agents 1.8.2:
  - `room_io.RoomOptions(participant_identity=..., delete_room_on_close=True)`.
  - `session.interrupt()` raises `RuntimeError` when speech can't be interrupted.
  - The default text input interrupts, then calls `generate_reply(user_input=...)`.
- livekit-server 1.13.7: the release checksum is pinned in CI.
- Tooling: TypeScript 7.0.2, Tailwind CSS 4.3.3 (`@tailwindcss/postcss`), Vitest 5.0.1, Playwright Test 1.63.0 and pnpm 10.33.0.

## 12. The api refuses default secrets in production
With `ULTRADEMO_ENVIRONMENT=production`, the api does not start while the internal token, the receipt secret or the LiveKit secret is empty or still a development default.

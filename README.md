# ultrademo

Live AI product experts. A viewer opens a demo link and talks to a voice agent. The agent runs the customer's real product in a browser, shares that screen, and explains it while it clicks. The design is in the blueprint (`/mnt/project-files/ultrademo/docs/`, v2 of 2026-09-22).

This repository holds the core demo loop, the control plane behind it, and the browser player a viewer opens.

## How a session works

```
viewer (apps/web player) ──POST /api/sessions──▶ web ──▶ api ──▶ Postgres (session row, RLS)
   │                                                      │
   │◀── LiveKit token (dispatches the agent by name) + receipt ──┘
   ▼
LiveKit room ◀── session-agent job ──GET context──▶ api
   ▲                │  Claude brain (tools, cache, effort)
   │                └──POST /v1/sandboxes──▶ operator ──▶ Chromium (Playwright)
   └──────────── screen-share track ◀──────────────────────┘
```

| Path | What it is |
|---|---|
| `packages/protocol` | Wire contracts shared by all services: realtime envelope and topics, the agent's tool surface (strict Claude tool schemas + pydantic validation), policy classes |
| `services/api` | FastAPI control plane. Client API v1 (launch configs, context links, a Supersonik-compatible `/demos` alias, sessions, transcripts), the public session start, and internal endpoints for the agent. asyncpg + row-level security |
| `services/operator` | Browser sandboxes. Playwright AI-mode ARIA snapshots with element refs, a server-side action policy (allowed / confirm / blocked), a domain allowlist, popup folding, and screen streaming into LiveKit (`VideoSource(is_screencast=True)`) |
| `services/session-agent` | LiveKit Agents worker. Silero VAD, the LiveKit turn detector, Deepgram STT and ElevenLabs TTS, with the LLM step replaced by the Claude brain through the documented `llm_node` override |
| `apps/web` | The player (Next.js 16). `/d/[slug]` and `/d/[slug]/embed`: a pre-call form and AI disclosure, the shared screen with player-drawn cursor and highlights, live captions and a transcript, typed chat, "point at the screen", a confirm dialog for risky clicks, CTA buttons, and post-call feedback |
| `db/migrations` | Plain SQL migrations. Postgres 18 `uuidv7()`, with a fallback for 16/17 |
| `e2e/` | The scripted agent and database setup for the browser test in `apps/web/e2e` |
| `examples/` | A sample CRM product and a bootstrap spec for onboarding a design partner |

## Choices that keep cost down and ROI up

- **One Claude conversation per session, cache-first.** The tools and platform rules are frozen and sit behind an explicit cache breakpoint, so they are read from cache across every session of an agent. The conversation tail uses automatic caching. Old screen snapshots are cleared by context editing once the prompt passes 60k tokens. Effort defaults to `low` for voice turns.
- **The agent speaks while it acts.** The brain yields a TTS flush between the spoken preamble and the tool calls, so the viewer hears "let me open approvals" while the click happens.
- **No duplicate model calls.** LiveKit's preemptive generation is off, because it would bill a second Claude request per turn.
- **Screens cost little when static.** Chrome's screencast only emits frames on repaint. While nothing changes, the streamer resends the last frame every 2 s so late joiners and the encoder still get a picture.
- **Safety isn't left to the prompt.** Mutating clicks go to the viewer for approval over LiveKit RPC. The model's tool schema has no way to approve its own action. Off-domain navigations are answered with a 204, so the page stays put.
- **Explicit capacity.** The operator answers 503 when it is full, so it scales out instead of degrading every call. Idle sandboxes are reaped.
- **The viewer can point.** Clicking the shared screen asks the agent "what's this?". The operator answers from the same accessibility snapshot the model acts on.
- **Every click on a CTA is a row.** The player reports CTA clicks and feedback with a signed session receipt, and each one lands in the outbox for the follow-up slice.
- **Rows that sales can act on.** Every session records its transcript, actions, cost ledger, validity and a summary for the sales team. Everything reaches the Client API with the same field names as the incumbent, so integrations can switch by changing the base URL.

## Run the tests

```bash
uv sync --all-packages
# A Postgres you can create databases on (16+; CI uses 18):
export ULTRADEMO_TEST_ADMIN_DSN=postgresql://postgres@localhost:5432/postgres
# Only if Playwright's own Chromium isn't installed:
export ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE=/path/to/chromium
uv run pytest -q
uv run ruff check . && uv run ruff format --check .

# The player
cd apps/web && corepack enable pnpm && pnpm install
pnpm run lint && pnpm run typecheck && pnpm run test && pnpm run build

# A whole demo call in a browser. Needs livekit-server on PATH (v1.13.7) and the Postgres above;
# a scripted agent stands in for the voice pipeline and Claude, so no API keys are needed.
# One-time: the browsers for the operator (Python) and for the test (Node).
uv run playwright install chromium && pnpm exec playwright install chromium
pnpm run build && pnpm run e2e
```

## Run a live demo locally

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY, ELEVEN_API_KEY and DEEPGRAM_API_KEY
docker compose up --build
docker compose exec api python -m ultrademo_api.bootstrap examples/acme.json   # prints an API key once
open http://localhost:3000/d/acme-crm-demo
```

- Without a Deepgram key, set `ULTRADEMO_AGENT_STT_PROVIDER=elevenlabs` in `.env`. Speech to text then uses ElevenLabs Scribe v2 Realtime with the same `ELEVEN_API_KEY`.
- On Docker Desktop (Mac, Windows), set `LIVEKIT_NODE_IP` to your computer's local IP (`ipconfig getifaddr en0` on a Mac). Otherwise the call joins with no screen and no sound.

To embed the demo on another site, add its origin to the launch config's `embed_origins` and frame `/d/acme-crm-demo/embed`. The page posts `ready`, `session.started`, `cta.clicked`, `handoff.requested` and `session.ended` to the host.

## Not built yet (next slices, in order)

1. **Post-call value:** a follow-up email with the rep's calendar link, a Slack alert with the summary, webhooks from the outbox, and insights.
2. **Product knowledge:** a product-map crawl, knowledge search, and drift checks.
3. **MCP server** at parity with the incumbent's 16 tools.
4. **Evals:** persona simulator runs against the brain, reusing `Brain` with a simulated viewer.
5. **Deploy:** container images to a managed runtime first. EKS, Temporal and multi-region come later, when load justifies them.

Decisions made while building are in `docs/adr/` (0014 for the first slice, 0015 for the player).

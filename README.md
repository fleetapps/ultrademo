# ultrademo

Live AI product experts. A viewer opens a demo link and talks to a voice agent. The agent runs the customer's real product in a browser, shares that screen, and explains it while it clicks. The design is in the blueprint (`/mnt/project-files/ultrademo/docs/`, v2 of 2026-09-22).

This repository is the **first vertical slice**: the core demo loop and the control plane behind it.

## How a session works

```
viewer ──POST /v1/public/sessions──▶ api ──▶ Postgres (session row, RLS)
   │                                  │
   │◀── LiveKit token (dispatches the agent by name) ──┘
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
| `db/migrations` | Plain SQL migrations. Postgres 18 `uuidv7()`, with a fallback for 16/17 |
| `examples/` | A sample CRM product and a bootstrap spec for onboarding a design partner |

## Choices that keep cost down and ROI up

- **One Claude conversation per session, cache-first.** The tools and platform rules are frozen and sit behind an explicit cache breakpoint, so they are read from cache across every session of an agent. The conversation tail uses automatic caching. Old screen snapshots are cleared by context editing once the prompt passes 60k tokens. Effort defaults to `low` for voice turns.
- **The agent speaks while it acts.** The brain yields a TTS flush between the spoken preamble and the tool calls, so the viewer hears "let me open approvals" while the click happens.
- **No duplicate model calls.** LiveKit's preemptive generation is off, because it would bill a second Claude request per turn.
- **Screens cost almost nothing when static.** Chrome's screencast only emits frames on repaint, and screencast mode sends nothing when the picture doesn't change.
- **Safety isn't left to the prompt.** Mutating clicks go to the viewer for approval over LiveKit RPC. The model's tool schema has no way to approve its own action. Off-domain navigations are answered with a 204, so the page stays put.
- **Explicit capacity.** The operator answers 503 when it is full, so it scales out instead of degrading every call. Idle sandboxes are reaped.
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
```

## Run a live demo locally

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY, DEEPGRAM_API_KEY, ELEVEN_API_KEY
docker compose up --build
docker compose exec api python -m ultrademo_api.bootstrap examples/acme.json   # prints an API key once
curl -s -X POST localhost:8000/v1/public/sessions -H 'content-type: application/json' \
     -d '{"slug":"acme-crm-demo"}'
```

The response carries a LiveKit URL and a viewer token. The browser player that joins with them is the next slice (`apps/web`). Until it exists, join with any LiveKit client that accepts a server URL and token.

## Not built yet (next slices, in order)

1. **`apps/web` player** (Next.js 16): `/d/[slug]`, the join flow, captions from text streams, a confirm dialog for the `ultrademo.confirm` RPC, CTA buttons, and client-side overlays.
2. **Post-call value:** a follow-up email with the rep's calendar link, a Slack alert with the summary, webhooks from the outbox, and insights.
3. **Product knowledge:** a product-map crawl, knowledge search, and drift checks.
4. **MCP server** at parity with the incumbent's 16 tools.
5. **Evals:** persona simulator runs against the brain, reusing `Brain` with a simulated viewer.
6. **Deploy:** container images to a managed runtime first. EKS, Temporal and multi-region come later, when load justifies them.

Decisions made while building this slice are in `docs/adr/0014-first-slice.md`.

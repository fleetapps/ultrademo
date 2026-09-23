# Browser end-to-end test

`apps/web/e2e/call.spec.ts` runs one whole demo call in Chromium against the real stack, started by
`apps/web/playwright.config.ts`: a LiveKit server in dev mode, the api on a fresh `ultrademo_e2e`
database (`prepare_db.py` loads `spec.json`), the operator with `examples/sample-product`, the
standalone Next server, and `scripted_agent.py` on the real LiveKit Agents worker.

The scripted agent replaces only the voice pipeline and Claude, so the test needs no API keys. See
ADR 15 §9 for what it covers and what it doesn't.

```bash
uv sync --all-packages
export ULTRADEMO_TEST_ADMIN_DSN=postgresql://postgres@localhost:5432/postgres
export PATH="/path/to/livekit-server-dir:$PATH"   # v1.13.7, or set LIVEKIT_SERVER_BIN
cd apps/web && pnpm install && pnpm run build && pnpm run e2e
```

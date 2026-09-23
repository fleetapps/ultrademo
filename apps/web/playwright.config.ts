import { defineConfig, devices } from "@playwright/test";

// Browser end-to-end test of a whole demo call (e2e/README.md at the repo root). It runs the real
// LiveKit server, api, operator and Next.js player, with a scripted agent on the real LiveKit
// Agents worker in place of the voice pipeline and Claude, so it needs no API keys.
//
// Needs: `livekit-server` on PATH (or LIVEKIT_SERVER_BIN), a Postgres admin DSN in
// ULTRADEMO_TEST_ADMIN_DSN, `uv sync --all-packages` at the repo root, and `next build` first.

const root = "../..";
const internal = { ULTRADEMO_INTERNAL_TOKEN: "e2e-internal-token" };
const livekit = { url: "ws://localhost:7880", key: "devkey", secret: "secret" };
const chromium = process.env.ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE;

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
    video: "retain-on-failure",
    permissions: ["microphone"],
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          ...(chromium ? { executablePath: chromium } : {}),
          args: [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
          ],
        },
      },
    },
  ],
  webServer: [
    {
      name: "livekit",
      command: `${process.env.LIVEKIT_SERVER_BIN ?? "livekit-server"} --dev --bind 127.0.0.1`,
      url: "http://localhost:7880",
      reuseExistingServer: false,
    },
    {
      name: "sample-product",
      command: "python3 -m http.server 8080 --bind 127.0.0.1 --directory examples/sample-product",
      cwd: root,
      url: "http://localhost:8080/index.html",
    },
    {
      name: "api",
      command:
        "export ULTRADEMO_DATABASE_URL=$(uv run python e2e/prepare_db.py) && " +
        "uv run uvicorn ultrademo_api.app:create_app --factory --port 8000",
      cwd: root,
      url: "http://localhost:8000/healthz",
      env: {
        ...internal,
        ULTRADEMO_PUBLIC_BASE_URL: "http://localhost:3000",
        ULTRADEMO_LIVEKIT_URL: livekit.url,
        ULTRADEMO_LIVEKIT_API_KEY: livekit.key,
        ULTRADEMO_LIVEKIT_API_SECRET: livekit.secret,
        ULTRADEMO_RECEIPT_SECRET: "e2e-receipt-secret",
      },
      timeout: 120_000,
    },
    {
      name: "operator",
      command: "uv run uvicorn ultrademo_operator.app:create_app --factory --port 8100",
      cwd: root,
      url: "http://localhost:8100/healthz",
      env: {
        ULTRADEMO_OPERATOR_INTERNAL_TOKEN: internal.ULTRADEMO_INTERNAL_TOKEN,
        ULTRADEMO_OPERATOR_LIVEKIT_URL: livekit.url,
        ULTRADEMO_OPERATOR_LIVEKIT_API_KEY: livekit.key,
        ULTRADEMO_OPERATOR_LIVEKIT_API_SECRET: livekit.secret,
        ...(chromium ? { ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE: chromium } : {}),
      },
    },
    {
      name: "agent",
      command: "uv run python -m livekit.agents start e2e/scripted_agent.py",
      cwd: root,
      // Logs go to stdout; ready once LiveKit has accepted the worker, so dispatch cannot race it.
      wait: { stdout: /registered worker/ },
      stdout: "pipe",
      env: {
        ULTRADEMO_AGENT_INTERNAL_TOKEN: internal.ULTRADEMO_INTERNAL_TOKEN,
        ULTRADEMO_AGENT_API_URL: "http://localhost:8000",
        ULTRADEMO_AGENT_OPERATOR_URL: "http://localhost:8100",
        ULTRADEMO_AGENT_CONFIRM_TIMEOUT_S: "20",
        LIVEKIT_URL: livekit.url,
        LIVEKIT_API_KEY: livekit.key,
        LIVEKIT_API_SECRET: livekit.secret,
      },
      timeout: 120_000,
    },
    {
      name: "web",
      // The standalone server (next.config.ts), as deployed; it serves .next/static once copied in.
      command:
        "cp -r .next/static .next/standalone/.next/ && PORT=3000 HOSTNAME=127.0.0.1 node .next/standalone/server.js",
      url: "http://localhost:3000/healthz",
      env: {
        ULTRADEMO_API_URL: "http://localhost:8000",
        ULTRADEMO_LIVEKIT_URL: livekit.url,
      },
    },
  ],
});

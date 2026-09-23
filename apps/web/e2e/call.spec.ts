import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";

// One whole demo call through the real stack (see playwright.config.ts). The scripted agent answers
// the typed commands "open hooli", "save it", "pricing" and "bye" (e2e/scripted_agent.py).

const SLUG = "e2e-demo";

function dbRow(sql: string): unknown {
  // The api's own driver reads back what the call stored, so the test checks the rows, not just the UI.
  const script = [
    "import asyncio, json, os, asyncpg",
    "from urllib.parse import urlsplit",
    "async def main():",
    "    admin = urlsplit(os.environ['ULTRADEMO_TEST_ADMIN_DSN'])",
    "    conn = await asyncpg.connect(admin._replace(path='/ultrademo_e2e').geturl())",
    "    row = await conn.fetchrow(os.environ['SQL'])",
    "    print(json.dumps(dict(row) if row else None, default=str))",
    "    await conn.close()",
    "asyncio.run(main())",
  ].join("\n");
  const out = execFileSync("uv", ["run", "python", "-c", script], {
    cwd: "../..",
    env: { ...process.env, SQL: sql },
    encoding: "utf8",
  });
  return JSON.parse(out.trim().split("\n").at(-1) ?? "null");
}

test("a viewer runs a whole demo call", async ({ page, context }) => {
  // CTA links open in a new tab; answer them locally instead of leaving the machine.
  await context.route("https://cal.example/**", (route) => route.fulfill({ body: "calendar" }));

  const res = await page.goto(`/d/${SLUG}`);
  expect(res?.status()).toBe(200);
  const headers = res?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["content-security-policy"]).toContain("ws://localhost:7880");
  expect(headers["x-frame-options"]).toBe("DENY");

  // Pre-call: branding, AI disclosure and the form from the launch config.
  await expect(page.getByRole("heading", { name: "Acme CRM live demo" })).toBeVisible();
  await expect(page.getByText("an AI agent")).toBeVisible();
  await page.getByLabel("Work email").fill("viewer@example.com");
  await page.getByLabel("Team size").selectOption("11-50");
  await page.getByTestId("start").click();

  // The agent joins, greets, and the product screen arrives as video.
  await expect(page.getByTestId("caption")).toContainText("Hi, I'm Ava");
  const transcript = page.getByTestId("transcript");
  await expect(transcript).toContainText("Show me a deal"); // viewer speech, published on their behalf
  await expect
    .poll(() => page.getByTestId("screen").evaluate((v: HTMLVideoElement) => v.videoWidth))
    .toBeGreaterThan(0);
  await expect(page.getByTestId("agent-state")).toHaveAttribute("data-state", "listening");

  const chat = page.getByTestId("chat-input");
  const say = async (text: string) => {
    await chat.fill(text);
    await chat.press("Enter");
    await expect(transcript).toContainText(text);
  };

  // The agent operates the product: cursor and highlight are drawn by the player.
  await say("open hooli");
  const highlight = page.getByTestId("highlight");
  await expect(highlight).toContainText("Save the deal here");
  await expect(page.getByTestId("cursor")).toBeVisible();
  await expect(page.getByTestId("caption")).toContainText("This is the Hooli renewal");

  // A mutating click waits for the viewer's approval.
  await say("save it");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("confirm-summary")).toContainText("Save deal");
  await dialog.getByRole("button", { name: "Allow", exact: true }).click();
  await expect(page.getByTestId("caption")).toContainText("Saved.");

  // The viewer points at the highlighted button and asks about it.
  const box = await highlight.boundingBox();
  const stage = await page.getByTestId("stage").boundingBox();
  if (!box || !stage) throw new Error("highlight or stage has no box");
  await page.getByTestId("point").click();
  // The target covers the whole stage, so the click position is relative to the stage.
  await page.getByTestId("point-target").click({
    position: { x: box.x - stage.x + box.width / 2, y: box.y - stage.y + box.height / 2 },
  });
  await expect(page.getByRole("status").filter({ hasText: "You pointed at" })).toContainText("Save deal");
  await expect(page.getByTestId("caption")).toContainText('pointing at the button "Save deal"');

  // The agent promotes a CTA; clicking it is recorded.
  await say("pricing");
  const book = page.getByTestId("cta-book-0");
  await expect(book).toHaveAttribute("data-primary", "");
  const recorded = page.waitForResponse((r) => r.url().includes("/events") && r.request().method() === "POST");
  const popup = context.waitForEvent("page");
  await book.click();
  expect((await recorded).status()).toBe(204);
  await (await popup).close();

  // The agent ends the call; the viewer rates it.
  await say("bye");
  await expect(page.getByTestId("post-call")).toBeVisible();
  await page.locator("label").filter({ hasText: "5 out of 5" }).click();
  await expect(page.getByRole("radio", { name: "5 out of 5" })).toBeChecked();
  await page.getByRole("button", { name: "Send feedback" }).click();
  await expect(page.getByText("Thanks for the feedback.")).toBeVisible();

  // What the call stored.
  await expect
    .poll(() => dbRow("SELECT status, end_reason FROM sessions ORDER BY created_at DESC LIMIT 1"))
    .toMatchObject({ status: "ended", end_reason: "agent_ended" });
  expect(
    dbRow(
      "SELECT count(*) FILTER (WHERE type = 'cta.clicked') AS ctas," +
        " count(*) FILTER (WHERE type = 'feedback') AS feedback FROM session_events",
    ),
  ).toEqual({ ctas: 1, feedback: 1 });
  // observe, click, observe, highlight, and the approved save.
  expect(dbRow("SELECT count(*) AS n FROM agent_actions WHERE status = 'ok'")).toEqual({ n: 5 });
});

test("the embed page may only be framed by the launch config's origins", async ({ page }) => {
  const res = await page.goto(`/d/${SLUG}/embed`);
  expect(res?.status()).toBe(200);
  const headers = res?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("frame-ancestors http://localhost:3999");
  expect(headers["x-frame-options"]).toBeUndefined();
});

test("an unknown demo is a 404", async ({ page }) => {
  const res = await page.goto("/d/no-such-demo");
  expect(res?.status()).toBe(404);
  await expect(page.getByText("This demo isn't available.")).toBeVisible();
});

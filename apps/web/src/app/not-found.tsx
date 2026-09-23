import { connection } from "next/server";
import { t } from "@/lib/strings";

// Rendered per request like every other page, so it carries the request's CSP nonce.
export default async function NotFound() {
  await connection();
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col justify-center gap-3 p-6 text-center">
      <h1 className="text-2xl font-semibold">{t.notLive}</h1>
      <p className="text-[var(--muted)]">Check the link, or ask the person who sent it for a new one.</p>
    </main>
  );
}

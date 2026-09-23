import { connection } from "next/server";

export default async function Home() {
  // Rendered per request so the CSP nonce reaches Next's scripts (see proxy.ts).
  await connection();
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col justify-center gap-3 p-6">
      <h1 className="text-3xl font-semibold tracking-tight">ultrademo</h1>
      <p className="text-[var(--muted)]">
        Live product demos with an AI product expert. Open a demo link such as <code>/d/your-demo</code> to start.
      </p>
    </main>
  );
}

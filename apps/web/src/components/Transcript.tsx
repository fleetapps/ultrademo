"use client";

import { useEffect, useRef } from "react";
import { t } from "@/lib/strings";
import type { Line } from "@/lib/transcript";

// How close to the bottom still counts as reading the newest lines.
const FOLLOW_SLACK_PX = 48;

export function Transcript({ lines, agentName }: { lines: Line[]; agentName: string }) {
  const list = useRef<HTMLOListElement>(null);
  // Whether the viewer is reading the newest lines; scrolling up to reread pauses following.
  const pinned = useRef(true);
  const count = lines.length;
  const last = lines.at(-1)?.text;
  // Follow the newest line. Scrolls only the list, never the page (the player may be embedded).
  useEffect(() => {
    const el = list.current;
    if (el && pinned.current && (count || last)) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [count, last]);
  const lastTop = useRef(0);
  const onScroll = () => {
    const el = list.current;
    if (!el) return;
    // Only an upward scroll unpins, so our own smooth scroll down never pauses following.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK_PX) pinned.current = true;
    else if (el.scrollTop < lastTop.current) pinned.current = false;
    lastTop.current = el.scrollTop;
  };

  return (
    <section aria-label={t.transcript} className="flex min-h-0 flex-1 flex-col">
      <h2 className="px-4 pt-3 pb-2 text-xs font-semibold tracking-wide text-[var(--muted)] uppercase">
        {t.transcript}
      </h2>
      <ol
        ref={list}
        onScroll={onScroll}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-4 text-sm"
        data-testid="transcript"
      >
        {lines.map((l) => (
          <li key={l.id} className={l.speaker === "viewer" ? "text-right" : ""}>
            <span className="mb-0.5 block text-[11px] font-medium text-[var(--muted)]">
              {l.speaker === "agent" ? agentName : "You"}
            </span>
            <span
              className={`inline-block max-w-[92%] rounded-2xl px-3 py-2 text-left leading-snug ${
                l.speaker === "agent" ? "bg-[var(--surface-2)]" : "bg-[var(--accent)] text-[var(--on-accent)]"
              } ${l.final ? "" : "opacity-70"}`}
            >
              {l.text}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

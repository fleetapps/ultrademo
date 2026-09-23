"use client";

import { useEffect, useRef, useState } from "react";

import type { ConfirmPrompt } from "@/lib/session";
import { t } from "@/lib/strings";

/**
 * The agent asks before a mutating action (policy class `confirm`). A native modal <dialog> gives
 * focus trapping and Escape handling; Escape and the timeout both count as "don't allow". Focus
 * lands on the first button, "Don't allow", so a stray Enter never approves.
 */
export function ConfirmDialog({
  prompt,
  onAnswer,
}: {
  prompt: ConfirmPrompt | null;
  onAnswer(approved: boolean): void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [left, setLeft] = useState(0);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (prompt && !d.open) d.showModal();
    if (!prompt && d.open) d.close();
  }, [prompt]);

  useEffect(() => {
    if (!prompt) return;
    const tick = () => setLeft(Math.max(0, Math.ceil((prompt.deadline - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [prompt]);

  return (
    <dialog
      ref={ref}
      aria-labelledby="ud-confirm-title"
      className="m-auto w-[min(92vw,420px)] rounded-2xl bg-[var(--surface)] p-0 text-[var(--text)] shadow-2xl backdrop:bg-black/40"
      onCancel={(e) => {
        e.preventDefault();
        onAnswer(false);
      }}
    >
      {prompt && (
        <div className="space-y-4 p-6">
          <h2 id="ud-confirm-title" className="text-lg font-semibold">
            {t.confirmTitle}
          </h2>
          <p className="text-sm text-[var(--muted)]">{t.confirmBody}</p>
          <p className="rounded-lg bg-[var(--surface-2)] px-3 py-2 font-medium" data-testid="confirm-summary">
            {prompt.summary}
          </p>
          <div className="flex items-center justify-end gap-2">
            <span className="mr-auto text-xs text-[var(--muted)] tabular-nums" aria-live="off">
              {left}s
            </span>
            <button type="button" className="ud-btn-secondary" onClick={() => onAnswer(false)}>
              {t.decline}
            </button>
            <button type="button" className="ud-btn-primary" onClick={() => onAnswer(true)}>
              {t.approve}
            </button>
          </div>
        </div>
      )}
    </dialog>
  );
}

"use client";

import { type FormEvent, useState } from "react";

import type { Cta } from "@/lib/ctas";
import { t } from "@/lib/strings";
import { CtaButton } from "./CtaButton";

interface Props {
  ctas: Cta[];
  canRate: boolean;
  onCta(cta: Cta): void;
  onFeedback(rating: number, comment: string): Promise<boolean>;
  onRestart(): void;
}

export function PostCall({ ctas, canRate, onCta, onFeedback, onRestart }: Props) {
  const [rating, setRating] = useState(0);
  const [comment, setComment] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!rating) return;
    setBusy(true);
    setSent(await onFeedback(rating, comment));
    setBusy(false);
  };

  return (
    <div className="mx-auto flex min-h-[60vh] w-full max-w-lg flex-col justify-center p-6" data-testid="post-call">
      <div className="space-y-6 rounded-2xl bg-[var(--surface)] p-6 shadow-sm ring-1 ring-black/5 sm:p-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t.endedTitle}</h1>
          <p className="mt-2 text-[var(--muted)]">{t.endedBody}</p>
        </div>

        {ctas.length > 0 && (
          <div className="space-y-2">
            {ctas.map((c, i) => (
              <CtaButton key={c.id} cta={c} primary={i === 0} onClick={onCta} />
            ))}
          </div>
        )}

        {canRate &&
          (sent ? (
            <p role="status" className="text-sm font-medium">
              {t.feedbackThanks}
            </p>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              <fieldset>
                <legend className="mb-2 text-sm font-medium">{t.feedbackQuestion}</legend>
                <div className="flex gap-2">
                  {[1, 2, 3, 4, 5].map((n) => (
                    <label key={n} className="ud-rating">
                      <input
                        type="radio"
                        name="rating"
                        value={n}
                        className="sr-only"
                        checked={rating === n}
                        onChange={() => setRating(n)}
                      />
                      <span aria-hidden>{n}</span>
                      <span className="sr-only">{`${n} out of 5`}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <label className="block text-sm">
                <span className="mb-1 block text-[var(--muted)]">{t.feedbackComment}</span>
                <textarea
                  className="ud-input min-h-20"
                  maxLength={2000}
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                />
              </label>
              <button type="submit" className="ud-btn-secondary" disabled={!rating || busy}>
                {t.feedbackSend}
              </button>
            </form>
          ))}

        <button type="button" className="text-sm text-[var(--muted)] underline underline-offset-4" onClick={onRestart}>
          {t.restart}
        </button>
      </div>
    </div>
  );
}

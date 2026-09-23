"use client";

import type { Cta } from "@/lib/ctas";
import { isSafeUrl } from "@/lib/ctas";

/** A call to action from the launch config. Links open in a new tab, so the call keeps going. */
export function CtaButton({ cta, primary, onClick }: { cta: Cta; primary?: boolean; onClick(cta: Cta): void }) {
  const cls = primary ? "ud-btn-primary w-full" : "ud-btn-secondary w-full";
  if (isSafeUrl(cta.url)) {
    return (
      <a
        href={cta.url}
        target="_blank"
        rel="noopener noreferrer"
        className={cls}
        onClick={() => onClick(cta)}
        data-testid={`cta-${cta.id}`}
        data-primary={primary ? "" : undefined}
      >
        {cta.label}
      </a>
    );
  }
  return (
    <button
      type="button"
      className={cls}
      onClick={() => onClick(cta)}
      data-testid={`cta-${cta.id}`}
      data-primary={primary ? "" : undefined}
    >
      {cta.label}
    </button>
  );
}

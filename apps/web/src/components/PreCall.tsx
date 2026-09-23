"use client";

import { type FormEvent, useState } from "react";

import type { PublicLaunchConfig } from "@/lib/player-types";
import { t } from "@/lib/strings";

interface Props {
  config: PublicLaunchConfig;
  busy: boolean;
  error: string | null;
  compact: boolean;
  onStart(form: Record<string, string>): void;
}

export function PreCall({ config, busy, error, compact, onStart }: Props) {
  const fields = config.form?.fields ?? [];
  const [values, setValues] = useState<Record<string, string>>({});

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    onStart(values);
  };

  return (
    <div
      className={`mx-auto flex w-full max-w-lg flex-col justify-center ${compact ? "min-h-full p-5" : "min-h-[70vh] p-6"}`}
    >
      <div className="rounded-2xl bg-[var(--surface)] p-6 shadow-sm ring-1 ring-black/5 sm:p-8">
        {config.branding.logo_url && (
          // biome-ignore lint/performance/noImgElement: a customer's logo on any https host, not an optimisable asset
          <img src={config.branding.logo_url} alt="" className="mb-5 h-8 w-auto" />
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-balance">{config.name}</h1>
        {config.description && <p className="mt-2 text-[var(--muted)]">{config.description}</p>}

        <form className="mt-6 space-y-4" onSubmit={submit} noValidate={false}>
          {fields.map((f) => (
            <div key={f.name} className="block text-sm">
              <label htmlFor={`ud-f-${f.name}`} className="mb-1 block font-medium">
                {f.label}
                {f.required && (
                  <span aria-hidden className="text-[var(--muted)]">
                    {" "}
                    *
                  </span>
                )}
              </label>
              {f.type === "select" && f.options ? (
                <select
                  id={`ud-f-${f.name}`}
                  className="ud-input"
                  required={f.required}
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                >
                  <option value="" disabled={f.required}>
                    Choose…
                  </option>
                  {f.options.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={`ud-f-${f.name}`}
                  className="ud-input"
                  name={f.name}
                  type={f.type === "tel" ? "tel" : f.type === "email" ? "email" : "text"}
                  autoComplete={f.type === "email" ? "email" : f.type === "tel" ? "tel" : "on"}
                  required={f.required}
                  maxLength={500}
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                />
              )}
            </div>
          ))}

          <p className="text-sm text-[var(--muted)]">{t.disclosure(config.agent_name)}</p>
          <p className="text-xs text-[var(--muted)]">{t.micHint}</p>

          {error && (
            <p
              role="alert"
              className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
            >
              {error}
            </p>
          )}

          <button type="submit" className="ud-btn-primary w-full py-3 text-base" disabled={busy} data-testid="start">
            {busy ? t.starting : t.start}
          </button>
        </form>
      </div>
    </div>
  );
}

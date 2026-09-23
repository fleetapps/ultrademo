// Calls to action come from the launch config, which the company authored. The agent can only ask
// for one by kind (`ui_show_cta`), so a model can never put its own link or wording in front of the
// viewer: an unmatched request is ignored.

export interface Cta {
  id: string;
  kind: string;
  label: string;
  url?: string;
}

export function resolveCta(ctas: readonly Cta[], kind: string): Cta | undefined {
  return ctas.find((c) => c.kind === kind);
}

export function isSafeUrl(url: string | undefined): url is string {
  if (!url) return false;
  try {
    return new URL(url).protocol === "https:";
  } catch {
    return false;
  }
}

// Brand accent from the launch config, with a text colour that keeps WCAG AA contrast on it.

const HEX = /^#[0-9a-f]{6}$/i;
export const DEFAULT_ACCENT = "#0f7f82";

function channel(v: number): number {
  const c = v / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

export function accentVars(accent: string | undefined): Record<string, string> {
  const a = accent && HEX.test(accent) ? accent : DEFAULT_ACCENT;
  // White or pure black: for any colour one of the two reaches at least 4.58:1 (#111 does not).
  const on = contrast(a, "#ffffff") >= contrast(a, "#000000") ? "#ffffff" : "#000000";
  return { "--accent": a, "--on-accent": on };
}

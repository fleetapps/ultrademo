import type { BBox, Viewport } from "./protocol";

// The stage box has the same aspect ratio as the sandbox viewport, so viewport pixels map to
// percentages of the stage linearly and the overlay needs no layout measurement.

export interface PercentRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), hi);

export function rectPercent(b: BBox, vp: Viewport): PercentRect {
  const left = clamp((b.x / vp.w) * 100, 0, 100);
  const top = clamp((b.y / vp.h) * 100, 0, 100);
  return {
    left,
    top,
    width: clamp((b.w / vp.w) * 100, 0, 100 - left),
    height: clamp((b.h / vp.h) * 100, 0, 100 - top),
  };
}

export function pointPercent(x: number, y: number, vp: Viewport): { left: number; top: number } {
  return { left: clamp((x / vp.w) * 100, 0, 100), top: clamp((y / vp.h) * 100, 0, 100) };
}

/** A click on the stage, in the sandbox's viewport pixels; null when it falls outside. */
export function stagePointToViewport(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  vp: Viewport,
): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0) return null;
  const fx = (clientX - rect.left) / rect.width;
  const fy = (clientY - rect.top) / rect.height;
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
  return { x: Math.round(fx * vp.w), y: Math.round(fy * vp.h) };
}

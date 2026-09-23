"use client";

import type { RemoteVideoTrack } from "livekit-client";
import { type MouseEvent, useEffect, useRef, useState } from "react";
import { pointPercent, rectPercent, stagePointToViewport } from "@/lib/overlay";
import type { Viewport } from "@/lib/protocol";
import type { Cursor, Highlight, Pointed } from "@/lib/session";
import { t } from "@/lib/strings";

interface Props {
  screen: RemoteVideoTrack | null;
  viewport: Viewport;
  cursor: Cursor | null;
  highlights: Highlight[];
  pointed: Pointed | null;
  pointing: boolean;
  waitingLabel: string;
  onPoint(x: number, y: number): void;
}

/**
 * The shared product screen with client-drawn overlays (ADR 4). The box keeps the sandbox's
 * aspect ratio, so overlay coordinates are plain percentages of it.
 */
export function Stage({ screen, viewport, cursor, highlights, pointed, pointing, waitingLabel, onPoint }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const boxRef = useRef<HTMLElement>(null);
  const [hasFrame, setHasFrame] = useState(false);

  useEffect(() => {
    const el = videoRef.current;
    if (!el || !screen) {
      setHasFrame(false);
      return;
    }
    // attach() is required with adaptiveStream, which sizes the stream to this element.
    screen.attach(el);
    const onFrame = () => setHasFrame(true);
    el.addEventListener("loadeddata", onFrame);
    return () => {
      el.removeEventListener("loadeddata", onFrame);
      screen.detach(el);
    };
  }, [screen]);

  const click = (e: MouseEvent<HTMLButtonElement>) => {
    if (!boxRef.current) return;
    if (e.detail === 0) {
      // Activated from the keyboard: point at the centre; the viewer can say what they mean.
      onPoint(Math.round(viewport.w / 2), Math.round(viewport.h / 2));
      return;
    }
    const p = stagePointToViewport(e.clientX, e.clientY, boxRef.current.getBoundingClientRect(), viewport);
    if (p) onPoint(p.x, p.y);
  };

  const cursorPos = cursor ? pointPercent(cursor.x, cursor.y, cursor.screen) : null;

  return (
    <section
      ref={boxRef}
      data-testid="stage"
      className="relative w-full overflow-hidden rounded-xl bg-[var(--stage)] shadow-sm ring-1 ring-black/5"
      style={{ aspectRatio: `${viewport.w} / ${viewport.h}` }}
      aria-label="Shared product screen"
    >
      <video
        ref={videoRef}
        className="absolute inset-0 h-full w-full object-contain"
        autoPlay
        playsInline
        muted
        data-testid="screen"
      />
      {!hasFrame && (
        <div className="absolute inset-0 grid place-items-center text-sm text-white/80">
          <span className="flex items-center gap-2">
            <span className="size-2 animate-pulse rounded-full bg-white/70" aria-hidden />
            {waitingLabel}
          </span>
        </div>
      )}

      {highlights.map((h) => {
        const r = rectPercent(h.bbox, h.screen);
        return (
          <div
            key={h.id}
            data-testid="highlight"
            className="ud-highlight pointer-events-none absolute rounded-lg border-[3px] border-[var(--accent)]"
            style={{
              left: `calc(${r.left}% - 5px)`,
              top: `calc(${r.top}% - 5px)`,
              width: `calc(${r.width}% + 10px)`,
              height: `calc(${r.height}% + 10px)`,
            }}
          >
            {h.label && (
              <span className="absolute -top-8 left-[-3px] rounded-md bg-[var(--accent)] px-2 py-1 text-xs font-semibold whitespace-nowrap text-[var(--on-accent)] shadow">
                {h.label}
              </span>
            )}
          </div>
        );
      })}

      {cursorPos && (
        <div
          data-testid="cursor"
          aria-hidden
          className="ud-cursor pointer-events-none absolute"
          style={{ left: `${cursorPos.left}%`, top: `${cursorPos.top}%` }}
        >
          {cursor?.clickAt != null && (
            // Keyed by the click time, so each click restarts the one-shot ripple animation.
            <span key={cursor.clickAt} className="ud-ripple absolute rounded-full border-2 border-[var(--accent)]" />
          )}
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            className="-mt-[3px] -ml-[3px] drop-shadow"
            aria-hidden="true"
          >
            <path
              d="M3 2l7.5 19 2.4-7.6L20.5 11z"
              fill="#fff"
              stroke="#151B23"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
        </div>
      )}

      {pointed && (
        <div
          className="pointer-events-none absolute"
          style={{
            left: `${pointPercent(pointed.x, pointed.y, viewport).left}%`,
            top: `${pointPercent(pointed.x, pointed.y, viewport).top}%`,
          }}
        >
          <span className="ud-ripple absolute rounded-full border-2 border-white" />
          <span
            className="absolute top-3 left-3 rounded-md bg-black/80 px-2 py-1 text-xs whitespace-nowrap text-white"
            role="status"
          >
            {pointed.element ? t.pointedAt(pointed.element.name || pointed.element.role) : t.pointedNothing}
          </span>
        </div>
      )}

      {pointing && (
        <button
          type="button"
          data-testid="point-target"
          className="absolute inset-0 z-10 cursor-crosshair bg-white/5 outline-none ring-inset focus-visible:ring-4 focus-visible:ring-[var(--accent)]"
          aria-label={t.pointing}
          onClick={click}
          // biome-ignore lint/a11y/noAutofocus: the viewer just asked to point, so the target takes focus
          autoFocus
        />
      )}
    </section>
  );
}

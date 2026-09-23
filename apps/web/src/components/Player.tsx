"use client";

import { RoomAudioRenderer } from "@livekit/components-react";
import { type FormEvent, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import type { Cta } from "@/lib/ctas";
import { postToHost } from "@/lib/embed";
import type { LaunchParams, PublicLaunchConfig, StartResponse } from "@/lib/player-types";
import { DemoSession } from "@/lib/session";
import { t } from "@/lib/strings";
import { latestAgentLine } from "@/lib/transcript";
import { ConfirmDialog } from "./ConfirmDialog";
import { CtaButton } from "./CtaButton";
import { PostCall } from "./PostCall";
import { PreCall } from "./PreCall";
import { Stage } from "./Stage";
import { Transcript } from "./Transcript";

interface Props {
  config: PublicLaunchConfig;
  launch: LaunchParams;
  embed: boolean;
}

type View =
  | { kind: "precall"; error: string | null; busy: boolean }
  | { kind: "call"; session: DemoSession; started: StartResponse }
  | { kind: "post"; started: StartResponse; reason: string };

const VISITOR_KEY = "ultrademo.visitor";

function visitorId(): string | null {
  try {
    let id = localStorage.getItem(VISITOR_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(VISITOR_KEY, id);
    }
    return id;
  } catch {
    return null; // storage blocked: the session is simply not linked to earlier visits
  }
}

function locale(): string {
  const lang = (typeof navigator !== "undefined" && navigator.language) || "en";
  return /^[a-z]{2,3}(-[A-Z]{2})?$/.test(lang) ? lang : lang.slice(0, 2).toLowerCase() || "en";
}

async function reportEvent(started: StartResponse, event: Record<string, unknown>): Promise<boolean> {
  try {
    const res = await fetch(`/api/sessions/${started.session_id}/events`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${started.receipt}` },
      body: JSON.stringify(event),
      // Survives the page navigating away right after a CTA click.
      keepalive: true,
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function Player({ config, launch, embed }: Props) {
  const [view, setView] = useState<View>({ kind: "precall", error: null, busy: false });
  const hosts = config.embed_origins;

  useEffect(() => {
    if (embed) postToHost("ready", { slug: config.slug }, hosts);
  }, [embed, config.slug, hosts]);

  const start = useCallback(
    async (form: Record<string, string>) => {
      setView({ kind: "precall", error: null, busy: true });
      let started: StartResponse;
      try {
        const res = await fetch("/api/sessions", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            slug: config.slug,
            token: launch.token,
            params: launch.params,
            form,
            locale: locale(),
            visitor_id: visitorId(),
            test: launch.test,
          }),
        });
        const body = (await res.json().catch(() => ({}))) as Partial<StartResponse> & { detail?: unknown };
        if (!res.ok || !body.token || !body.livekit_url || !body.session_id || !body.receipt) {
          const detail = typeof body.detail === "string" ? body.detail : t.startFailed;
          setView({ kind: "precall", error: detail, busy: false });
          return;
        }
        started = body as StartResponse;
      } catch {
        setView({ kind: "precall", error: t.startFailed, busy: false });
        return;
      }

      const session = new DemoSession({
        url: started.livekit_url,
        token: started.token,
        ctas: config.ctas,
        hooks: {
          onLive: () => embed && postToHost("session.started", { session_id: started.session_id }, hosts),
          onEnded: (reason) => {
            if (embed) postToHost("session.ended", { session_id: started.session_id, reason }, hosts);
            setView({ kind: "post", started, reason });
          },
          onHandoff: () => embed && postToHost("handoff.requested", { session_id: started.session_id }, hosts),
        },
      });
      setView({ kind: "call", session, started });
      await session.start();
    },
    [config.slug, config.ctas, launch, embed, hosts],
  );

  const onCta = useCallback(
    (started: StartResponse, phase: "in_call" | "post_call") => (cta: Cta) => {
      void reportEvent(started, { type: "cta.clicked", cta_id: cta.id, label: cta.label, phase });
      if (embed) postToHost("cta.clicked", { session_id: started.session_id, cta_id: cta.id }, hosts);
    },
    [embed, hosts],
  );

  // Leave the room when the page goes away or the component unmounts.
  const current = view.kind === "call" ? view.session : null;
  useEffect(() => {
    if (!current) return;
    return () => current.dispose();
  }, [current]);

  if (view.kind === "precall") {
    return <PreCall config={config} busy={view.busy} error={view.error} compact={embed} onStart={start} />;
  }
  if (view.kind === "post") {
    const s = view.started;
    return (
      <PostCall
        ctas={config.ctas}
        canRate
        onCta={onCta(s, "post_call")}
        onFeedback={(rating, comment) => reportEvent(s, { type: "feedback", rating, comment })}
        onRestart={() => setView({ kind: "precall", error: null, busy: false })}
      />
    );
  }
  return <LiveCall config={config} session={view.session} embed={embed} onCta={onCta(view.started, "in_call")} />;
}

function LiveCall({
  config,
  session,
  embed,
  onCta,
}: {
  config: PublicLaunchConfig;
  session: DemoSession;
  embed: boolean;
  onCta(cta: Cta): void;
}) {
  // The call only exists after a click, so the server snapshot is never used for real.
  const s = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const [pointing, setPointing] = useState(false);
  const [chat, setChat] = useState("");

  const promoted = s.promotedCta;
  const railCtas = config.ctas;
  const latest = useMemo(() => latestAgentLine(s.lines), [s.lines]);
  const lastFinal = latest?.final ? latest : undefined;

  // Escape leaves pointing mode.
  useEffect(() => {
    if (!pointing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPointing(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pointing]);
  const lastAction = s.actions.at(-1);
  const live = s.phase === "live";

  const send = async (e: FormEvent) => {
    e.preventDefault();
    if (await session.sendChat(chat)) setChat("");
  };

  if (s.phase === "failed") {
    return (
      <div className="mx-auto max-w-lg p-6 text-center" role="alert">
        <p className="mb-4">{s.error === "agent_timeout" ? t.agentTimeout : t.startFailed}</p>
        <button type="button" className="ud-btn-primary" onClick={() => location.reload()}>
          {t.tryAgain}
        </button>
      </div>
    );
  }

  const stateLabel = s.agentState ? t.state[s.agentState] : t.waitingAgent(config.agent_name);

  return (
    <div
      className={`mx-auto grid w-full max-w-[1400px] gap-4 ${embed ? "p-2" : "p-3 sm:p-5"} min-[968px]:grid-cols-[minmax(0,1fr)_320px] min-[1200px]:grid-cols-[minmax(0,1fr)_360px]`}
    >
      <RoomAudioRenderer room={session.room} />

      <main className="flex min-w-0 flex-col gap-3">
        <header className="flex flex-wrap items-center gap-3">
          <h1 className="mr-auto truncate text-base font-semibold">{config.name}</h1>
          <span
            className="inline-flex items-center gap-2 rounded-full bg-[var(--surface)] px-3 py-1 text-xs font-medium ring-1 ring-black/5"
            data-testid="agent-state"
            data-state={s.agentState ?? "joining"}
          >
            <span
              className={`size-2 rounded-full ${s.agentState === "speaking" || s.agentState === "acting" ? "animate-pulse bg-[var(--accent)]" : "bg-[var(--muted)]"}`}
              aria-hidden
            />
            {s.connection === "reconnecting" ? t.reconnecting : `${config.agent_name} · ${stateLabel}`}
          </span>
          {s.startedAt && <Elapsed since={s.startedAt} />}
          <button type="button" className="ud-btn-danger" onClick={() => void session.end()} data-testid="end">
            {t.end}
          </button>
        </header>

        <Stage
          screen={s.screen}
          viewport={s.viewport}
          cursor={s.cursor}
          highlights={s.highlights}
          pointed={s.pointed}
          pointing={pointing && live}
          waitingLabel={live ? t.waitingScreen : t.waitingAgent(config.agent_name)}
          onPoint={(x, y) => {
            setPointing(false);
            void session.point(x, y, "click");
          }}
        />

        {/* Screen readers hear each finished line once, not every streamed word. */}
        <p className="sr-only" aria-live="polite">
          {lastFinal?.text}
        </p>
        <div
          className="min-h-12 rounded-xl bg-[var(--surface)] px-4 py-3 text-[15px] leading-snug ring-1 ring-black/5"
          data-testid="caption"
        >
          {latest ? latest.text : <span className="text-[var(--muted)]">{stateLabel}</span>}
          {lastAction?.status === "running" && (
            <span className="ml-2 inline-flex items-center gap-1 text-xs text-[var(--muted)]" data-testid="action">
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--accent)]" aria-hidden />
              {lastAction.label}
            </span>
          )}
        </div>

        {s.handoff && (
          <p role="status" className="rounded-xl bg-[var(--surface-2)] px-4 py-2 text-sm">
            {t.handoff}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={s.micEnabled ? "ud-btn-secondary" : "ud-btn-secondary line-through"}
            onClick={() => void session.setMic(!s.micEnabled)}
            disabled={!live}
            data-testid="mic"
          >
            {s.micEnabled ? t.mute : t.unmute}
          </button>
          <button
            type="button"
            className="ud-btn-secondary"
            onClick={() => setPointing((p) => !p)}
            disabled={!live || !s.screen}
            data-testid="point"
          >
            {pointing ? t.pointing : t.point}
          </button>
          {!s.canPlayAudio && (
            <button type="button" className="ud-btn-primary" onClick={() => void session.startAudio()}>
              {t.enableAudio}
            </button>
          )}
          <form onSubmit={send} className="flex min-w-[240px] flex-1 gap-2">
            <label htmlFor="ud-chat" className="sr-only">
              {t.chatPlaceholder}
            </label>
            <input
              id="ud-chat"
              className="ud-input flex-1"
              placeholder={t.chatPlaceholder}
              value={chat}
              maxLength={1000}
              onChange={(e) => setChat(e.target.value)}
              disabled={!live}
              data-testid="chat-input"
            />
            <button type="submit" className="ud-btn-secondary" disabled={!live || !chat.trim()}>
              {t.send}
            </button>
          </form>
        </div>
        {s.micError && (
          <p role="status" className="text-sm text-[var(--muted)]">
            {t.micBlocked}
          </p>
        )}
      </main>

      <aside className="flex min-h-0 flex-col overflow-hidden rounded-xl bg-[var(--surface)] ring-1 ring-black/5 min-[968px]:max-h-[calc(100dvh-2.5rem)]">
        {railCtas.length > 0 && (
          <div className="space-y-2 border-b border-black/5 p-4" data-testid="cta-rail">
            {railCtas.map((c) => (
              <CtaButton key={c.id} cta={c} primary={promoted ? promoted.id === c.id : false} onClick={onCta} />
            ))}
          </div>
        )}
        <Transcript lines={s.lines} agentName={config.agent_name} />
      </aside>

      <ConfirmDialog prompt={s.confirm} onAnswer={(ok) => session.answerConfirm(ok)} />
    </div>
  );
}

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(since);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const s = Math.max(0, Math.floor((now - since) / 1000));
  return (
    <span className="text-xs text-[var(--muted)] tabular-nums" role="timer" aria-label="Elapsed time">
      {Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")}
    </span>
  );
}

// One live demo call, as a plain class around livekit-client's Room.
//
// React renders `getSnapshot()` through useSyncExternalStore; nothing here depends on React, so
// the realtime behaviour is testable without a DOM renderer and immune to effect double-runs.
//
// Who may send what (checked on every message, never assumed):
// - the agent (ParticipantKind.AGENT): agent state, action/CTA/status events, the confirm RPC;
// - the sandbox (attribute ultrademo.kind = "sandbox"): the screen track, screen meta, overlays;
// - transcripts arrive on lk.transcription from the agent, attributed to whoever spoke.

import {
  ConnectionState,
  DisconnectReason,
  type Participant,
  ParticipantKind,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteVideoTrack,
  Room,
  RoomEvent,
  RpcError,
  type RpcInvocationData,
  type TextStreamReader,
  Track,
} from "livekit-client";

import { type Cta, resolveCta } from "./ctas";
import {
  type AgentState,
  ATTR_AGENT_STATE,
  ATTR_KIND,
  ATTR_SCREEN_META,
  ATTR_SEGMENT_ID,
  ATTR_TRANSCRIPTION_FINAL,
  type BBox,
  DEFAULT_VIEWPORT,
  type ElementRef,
  parseAgentState,
  parseConfirmRequest,
  parseElement,
  parseEvent,
  parseScreenMeta,
  RPC_CONFIRM,
  RPC_POINTER,
  type ServerEvent,
  TOPIC_CHAT,
  TOPIC_EVENTS,
  TOPIC_TRANSCRIPTION,
  type Viewport,
} from "./protocol";
import { describeTool } from "./strings";
import { type Line, upsertLine } from "./transcript";

export type Phase = "connecting" | "waiting_agent" | "live" | "ended" | "failed";
export type Connection = "connecting" | "connected" | "reconnecting" | "disconnected";

export interface ActionItem {
  id: number;
  tool: string;
  label: string;
  status: "running" | "ok" | "blocked" | "needs_confirmation" | "error";
  at: number;
}

export interface Highlight {
  id: string;
  bbox: BBox;
  label: string;
  screen: Viewport;
  expiresAt: number;
}

export interface Cursor {
  x: number;
  y: number;
  screen: Viewport;
  clickAt: number | null;
}

export interface ConfirmPrompt {
  id: string;
  summary: string;
  element: ElementRef | null;
  deadline: number;
}

export interface Pointed {
  element: ElementRef | null;
  x: number;
  y: number;
  at: number;
}

export interface SessionSnapshot {
  phase: Phase;
  connection: Connection;
  agentState: AgentState | null;
  screen: RemoteVideoTrack | null;
  viewport: Viewport;
  lines: Line[];
  actions: ActionItem[];
  cursor: Cursor | null;
  highlights: Highlight[];
  promotedCta: Cta | null;
  confirm: ConfirmPrompt | null;
  micEnabled: boolean;
  micError: string | null;
  canPlayAudio: boolean;
  handoff: boolean;
  pointed: Pointed | null;
  endReason: string | null;
  error: string | null;
  startedAt: number | null;
}

export interface SessionHooks {
  onLive?(): void;
  onEnded?(reason: string): void;
  onCtaShown?(cta: Cta): void;
  onHandoff?(): void;
}

export interface SessionOptions {
  url: string;
  token: string;
  ctas: readonly Cta[];
  hooks?: SessionHooks;
  agentJoinTimeoutMs?: number;
  /** For tests: a pre-built Room. */
  room?: Room;
}

const MAX_ACTIONS = 20;
const MAX_HIGHLIGHTS = 3;
const MAX_CHAT_CHARS = 1_000;
const CONFIRM_MARGIN_MS = 1_500;
// How long a stream from a not-yet-announced sender waits for the join announcement.
const ANNOUNCE_WAIT_MS = 3_000;
// How long the agent may be missing (a reconnect) before the call counts as over.
const AGENT_GONE_GRACE_MS = 8_000;
// App-defined RPC error codes must be outside the 1001-1999 range LiveKit reserves.
const RPC_NOT_ALLOWED = 3_001;

const INITIAL: SessionSnapshot = {
  phase: "connecting",
  connection: "connecting",
  agentState: null,
  screen: null,
  viewport: DEFAULT_VIEWPORT,
  lines: [],
  actions: [],
  cursor: null,
  highlights: [],
  promotedCta: null,
  confirm: null,
  micEnabled: false,
  micError: null,
  canPlayAudio: true,
  handoff: false,
  pointed: null,
  endReason: null,
  error: null,
  startedAt: null,
};

function isAgent(p: Participant): boolean {
  return p.kind === ParticipantKind.AGENT;
}

function isSandbox(p: Participant): boolean {
  return p.attributes[ATTR_KIND] === "sandbox";
}

function reasonName(reason: DisconnectReason | undefined): string {
  switch (reason) {
    case DisconnectReason.CLIENT_INITIATED:
      return "viewer_ended";
    case DisconnectReason.ROOM_DELETED:
    case DisconnectReason.ROOM_CLOSED:
      return "session_ended";
    case DisconnectReason.PARTICIPANT_REMOVED:
      return "removed";
    case DisconnectReason.DUPLICATE_IDENTITY:
      return "opened_elsewhere";
    default:
      return "connection_lost";
  }
}

export class DemoSession {
  readonly room: Room;
  private snap: SessionSnapshot = INITIAL;
  private readonly listeners = new Set<() => void>();
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private confirmResolver: ((approved: boolean) => void) | null = null;
  private agentIdentity: string | null = null;
  private actionSeq = 0;
  private chatSeq = 0;
  private disposed = false;

  constructor(private readonly opts: SessionOptions) {
    this.room =
      opts.room ??
      new Room({
        // Only fetch the screen at the size it is shown, and let publishers pause unused layers.
        adaptiveStream: true,
        dynacast: true,
        disconnectOnPageLeave: true,
      });
  }

  // --- store ------------------------------------------------------------------------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): SessionSnapshot => this.snap;

  private set(patch: Partial<SessionSnapshot>): void {
    if (this.disposed) return;
    this.snap = { ...this.snap, ...patch };
    for (const l of this.listeners) l();
  }

  private later(ms: number, fn: () => void): void {
    const t = setTimeout(() => {
      this.timers.delete(t);
      fn();
    }, ms);
    this.timers.add(t);
  }

  // --- lifecycle --------------------------------------------------------------------------------

  async start(): Promise<void> {
    const room = this.room;
    room
      .on(RoomEvent.ParticipantConnected, this.onParticipant)
      .on(RoomEvent.ParticipantDisconnected, this.onParticipantLeft)
      .on(RoomEvent.TrackSubscribed, this.onTrack)
      .on(RoomEvent.TrackUnsubscribed, this.onTrackGone)
      .on(RoomEvent.ParticipantAttributesChanged, this.onAttributes)
      .on(RoomEvent.DataReceived, this.onData)
      .on(RoomEvent.ConnectionStateChanged, this.onConnection)
      .on(RoomEvent.AudioPlaybackStatusChanged, this.onAudioPlayback)
      .on(RoomEvent.MediaDevicesError, this.onMediaError)
      .on(RoomEvent.Disconnected, this.onDisconnected);
    room.registerTextStreamHandler(TOPIC_TRANSCRIPTION, this.onTranscription);
    room.registerRpcMethod(RPC_CONFIRM, this.onConfirm);

    try {
      await room.connect(this.opts.url, this.opts.token);
    } catch (e) {
      this.fail(e instanceof Error ? e.message : "connect_failed");
      return;
    }
    for (const p of room.remoteParticipants.values()) this.onParticipant(p);
    this.set({ connection: "connected", phase: this.agentIdentity ? "live" : "waiting_agent" });
    if (!this.agentIdentity) {
      this.later(this.opts.agentJoinTimeoutMs ?? 25_000, () => {
        if (!this.agentIdentity && this.snap.phase === "waiting_agent") {
          this.fail("agent_timeout");
          void room.disconnect();
        }
      });
    }
    // The click that started the call is still a user activation, so audio may start now.
    await room.startAudio().catch(() => undefined);
    this.set({ canPlayAudio: room.canPlaybackAudio });
    await this.setMic(true);
  }

  /** The viewer ends the call. */
  async end(): Promise<void> {
    this.finish("viewer_ended");
    await this.room.disconnect();
  }

  dispose(): void {
    if (this.disposed) return;
    this.confirmResolver?.(false);
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.room.unregisterTextStreamHandler(TOPIC_TRANSCRIPTION);
    this.room.unregisterRpcMethod(RPC_CONFIRM);
    this.room.removeAllListeners();
    void this.room.disconnect();
    this.disposed = true;
    this.listeners.clear();
  }

  private fail(error: string): void {
    if (this.snap.phase === "ended" || this.snap.phase === "failed") return;
    this.confirmResolver?.(false);
    this.set({ phase: "failed", error, connection: "disconnected" });
  }

  private finish(reason: string): void {
    if (this.snap.phase === "ended" || this.snap.phase === "failed") return;
    this.confirmResolver?.(false);
    this.set({
      phase: "ended",
      endReason: reason,
      agentState: "ended",
      highlights: [],
      cursor: null,
      micEnabled: false,
    });
    this.opts.hooks?.onEnded?.(reason);
  }

  // --- viewer actions ---------------------------------------------------------------------------

  async setMic(enabled: boolean): Promise<void> {
    try {
      await this.room.localParticipant.setMicrophoneEnabled(enabled);
      this.set({ micEnabled: enabled, micError: null });
    } catch (e) {
      this.set({ micEnabled: false, micError: e instanceof Error ? e.name : "mic_error" });
    }
  }

  async startAudio(): Promise<void> {
    await this.room.startAudio().catch(() => undefined);
    this.set({ canPlayAudio: this.room.canPlaybackAudio });
  }

  async sendChat(raw: string): Promise<boolean> {
    const text = raw.trim().slice(0, MAX_CHAT_CHARS);
    if (!text || this.snap.phase !== "live") return false;
    await this.room.localParticipant.sendText(text, { topic: TOPIC_CHAT });
    this.chatSeq += 1;
    this.set({
      lines: upsertLine(this.snap.lines, {
        id: `chat_${this.chatSeq}`,
        speaker: "viewer",
        text,
        final: true,
        source: "chat",
        at: Date.now(),
      }),
    });
    return true;
  }

  answerConfirm(approved: boolean): void {
    this.confirmResolver?.(approved);
  }

  /** Ask the agent what is at a point of the shared screen (viewport pixels). */
  async point(x: number, y: number, kind: "point" | "click" = "click"): Promise<Pointed | null> {
    const agent = this.agentIdentity;
    if (!agent || this.snap.phase !== "live") return null;
    let element: ElementRef | null = null;
    try {
      const answer = await this.room.localParticipant.performRpc({
        destinationIdentity: agent,
        method: RPC_POINTER,
        payload: JSON.stringify({ x, y, kind }),
        responseTimeout: 8_000,
      });
      element = parseElement((JSON.parse(answer) as { element?: unknown }).element);
    } catch {
      element = null;
    }
    const pointed = { element, x, y, at: Date.now() };
    this.set({ pointed });
    this.later(4_000, () => {
      if (this.snap.pointed === pointed) this.set({ pointed: null });
    });
    return pointed;
  }

  // --- room events ------------------------------------------------------------------------------

  /**
   * Take up a participant the room already knows but this session has not seen announced yet:
   * data that arrived while connecting is delivered before `connect()` resolves.
   */
  private adopt(p: RemoteParticipant | undefined): void {
    if (p && isAgent(p) && !this.agentIdentity) this.onParticipant(p);
  }

  private onParticipant = (p: RemoteParticipant): void => {
    if (isAgent(p) && !this.agentIdentity) {
      this.agentIdentity = p.identity;
      this.set({
        agentState: parseAgentState(p.attributes[ATTR_AGENT_STATE]) ?? "idle",
        phase: this.snap.phase === "waiting_agent" ? "live" : this.snap.phase,
        startedAt: this.snap.startedAt ?? Date.now(),
      });
      this.opts.hooks?.onLive?.();
    }
    if (isSandbox(p)) {
      this.set({ viewport: parseScreenMeta(p.attributes[ATTR_SCREEN_META]) ?? this.snap.viewport });
    }
  };

  private onParticipantLeft = (p: RemoteParticipant): void => {
    if (p.identity !== this.agentIdentity) return;
    // A full reconnect announces every remote participant as gone before it rejoins them, and
    // the room still reads as connected at that moment. So wait before treating the agent as
    // gone; a real end arrives as `session.status` or the room closing, which end at once.
    this.later(AGENT_GONE_GRACE_MS, () => {
      const agent = this.agentIdentity;
      if (agent && !this.room.remoteParticipants.has(agent)) this.finish("agent_left");
    });
  };

  private onTrack = (track: RemoteTrack, pub: RemoteTrackPublication, p: RemoteParticipant): void => {
    if (track.kind === Track.Kind.Video && pub.source === Track.Source.ScreenShare && isSandbox(p)) {
      this.set({ screen: track as RemoteVideoTrack });
    }
  };

  private onTrackGone = (track: RemoteTrack): void => {
    if (this.snap.screen === track) this.set({ screen: null });
  };

  private onAttributes = (changed: Record<string, string>, p: Participant): void => {
    if (p.identity === this.agentIdentity && ATTR_AGENT_STATE in changed) {
      const state = parseAgentState(changed[ATTR_AGENT_STATE]);
      if (state) this.set({ agentState: state });
    }
    if (isSandbox(p) && ATTR_SCREEN_META in changed) {
      const vp = parseScreenMeta(changed[ATTR_SCREEN_META]);
      if (vp) this.set({ viewport: vp });
    }
  };

  private onConnection = (state: ConnectionState): void => {
    const map: Partial<Record<ConnectionState, Connection>> = {
      [ConnectionState.Connecting]: "connecting",
      [ConnectionState.Connected]: "connected",
      [ConnectionState.Reconnecting]: "reconnecting",
      [ConnectionState.SignalReconnecting]: "reconnecting",
      [ConnectionState.Disconnected]: "disconnected",
    };
    const c = map[state];
    if (c) this.set({ connection: c });
  };

  private onAudioPlayback = (): void => {
    this.set({ canPlayAudio: this.room.canPlaybackAudio });
  };

  private onMediaError = (e: Error): void => {
    this.set({ micEnabled: false, micError: e.name || "mic_error" });
  };

  private onDisconnected = (reason?: DisconnectReason): void => {
    this.set({ connection: "disconnected" });
    if (this.snap.phase === "live" || this.snap.phase === "waiting_agent") {
      this.finish(reasonName(reason));
    }
  };

  private onData = (payload: Uint8Array, p?: RemoteParticipant, _kind?: unknown, topic?: string): void => {
    if (topic !== TOPIC_EVENTS || !p) return;
    this.adopt(p);
    const ev = parseEvent(payload);
    if (!ev) return;
    if (ev.type.startsWith("overlay.")) {
      if (isSandbox(p)) this.applyOverlay(ev);
    } else if (p.identity === this.agentIdentity) {
      this.applyAgentEvent(ev);
    }
  };

  private applyOverlay(ev: ServerEvent): void {
    const now = Date.now();
    if (ev.type === "overlay.cursor") {
      this.set({
        cursor: {
          x: ev.x,
          y: ev.y,
          screen: ev.screen,
          clickAt: ev.kind === "click" ? now : (this.snap.cursor?.clickAt ?? null),
        },
      });
    } else if (ev.type === "overlay.highlight") {
      const hl: Highlight = {
        id: ev.id,
        bbox: ev.bbox,
        label: ev.label,
        screen: ev.screen,
        expiresAt: now + ev.ttlMs,
      };
      const rest = this.snap.highlights.filter((h) => h.id !== ev.id);
      this.set({ highlights: [...rest, hl].slice(-MAX_HIGHLIGHTS) });
      this.later(ev.ttlMs + 50, () => {
        const t = Date.now();
        this.set({ highlights: this.snap.highlights.filter((h) => h.expiresAt > t) });
      });
    } else if (ev.type === "overlay.clear") {
      this.set({ highlights: [] });
    }
  }

  private applyAgentEvent(ev: ServerEvent): void {
    switch (ev.type) {
      case "action.started": {
        this.actionSeq += 1;
        const item: ActionItem = {
          id: this.actionSeq,
          tool: ev.tool,
          label: describeTool(ev.tool),
          status: "running",
          at: Date.now(),
        };
        this.set({ actions: [...this.snap.actions, item].slice(-MAX_ACTIONS) });
        break;
      }
      case "action.completed":
      case "action.failed": {
        const actions = [...this.snap.actions];
        const i = actions.findLastIndex((a) => a.status === "running" && a.tool === ev.tool);
        const a = actions[i];
        if (a) {
          const status = (["ok", "blocked", "needs_confirmation", "error"] as const).find((s) => s === ev.status);
          actions[i] = {
            ...a,
            label: ev.label || a.label,
            status: status ?? (ev.type === "action.failed" ? "error" : "ok"),
          };
          this.set({ actions });
        }
        break;
      }
      case "cta.show": {
        const cta = resolveCta(this.opts.ctas, ev.kind);
        if (cta) {
          this.set({ promotedCta: cta });
          this.opts.hooks?.onCtaShown?.(cta);
        }
        break;
      }
      case "session.status":
        if (ev.status === "ended") this.finish(ev.reason || "agent_ended");
        else if (ev.status === "handoff_requested" && !this.snap.handoff) {
          this.set({ handoff: true });
          this.opts.hooks?.onHandoff?.();
        }
        break;
      default:
        break;
    }
  }

  /**
   * Who a transcription stream speaks for. Streams travel on the data channel and participant
   * announcements on the signal connection, so the agent's first words can arrive a moment before
   * the player knows the agent has joined; wait briefly for the announcement rather than drop them.
   */
  private async speakerOf(identity: string): Promise<Line["speaker"] | null> {
    if (identity === this.room.localParticipant.identity) return "viewer";
    this.adopt(this.room.remoteParticipants.get(identity));
    if (!this.agentIdentity && !this.room.remoteParticipants.has(identity)) {
      await new Promise<void>((resolve) => {
        const done = () => {
          clearTimeout(timer);
          this.timers.delete(timer);
          this.room.off(RoomEvent.ParticipantConnected, onJoin);
          resolve();
        };
        const onJoin = (p: RemoteParticipant) => {
          if (p.identity === identity) done();
        };
        const timer = setTimeout(done, ANNOUNCE_WAIT_MS);
        this.timers.add(timer);
        this.room.on(RoomEvent.ParticipantConnected, onJoin);
      });
    }
    // Nobody but the agent and the viewer speaks in a demo room.
    return identity === this.agentIdentity ? "agent" : null;
  }

  private onTranscription = (reader: TextStreamReader, info: { identity: string }): void => {
    const attrs = reader.info.attributes ?? {};
    const id = attrs[ATTR_SEGMENT_ID] || reader.info.id;
    const streamFinal = attrs[ATTR_TRANSCRIPTION_FINAL] === "true";
    const at = reader.info.timestamp || Date.now();
    let text = "";
    void (async () => {
      const speaker = await this.speakerOf(info.identity);
      if (!speaker || this.disposed) return;
      const push = (final: boolean) =>
        this.set({ lines: upsertLine(this.snap.lines, { id, speaker, text, final, at }) });
      try {
        for await (const chunk of reader) {
          text += chunk;
          push(streamFinal);
        }
        // The agent streams one segment per stream, so its line is done when the stream ends.
        // The viewer's interim results are separate streams, so only an explicit final counts.
        push(speaker === "agent" || streamFinal);
      } catch {
        // A stream cut by a disconnect keeps what arrived, as a finished line.
        if (text) push(true);
      }
    })();
  };

  private onConfirm = async (data: RpcInvocationData): Promise<string> => {
    if (data.callerIdentity !== this.agentIdentity) {
      throw new RpcError(RPC_NOT_ALLOWED, "Only the agent can ask for confirmation");
    }
    const req = parseConfirmRequest(data.payload);
    if (!req || this.snap.phase !== "live") return JSON.stringify({ approved: false });
    this.confirmResolver?.(false); // a newer request supersedes an unanswered one

    // Answer before the caller gives up; an unanswered prompt counts as declined.
    const timeoutMs = Math.max(1_000, data.responseTimeout - CONFIRM_MARGIN_MS);
    return new Promise<string>((resolve) => {
      let done = false;
      const settle = (approved: boolean) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this.timers.delete(timer);
        if (this.confirmResolver === settle) this.confirmResolver = null;
        if (this.snap.confirm?.id === data.requestId) this.set({ confirm: null });
        resolve(JSON.stringify({ approved }));
      };
      const timer = setTimeout(() => settle(false), timeoutMs);
      this.timers.add(timer);
      this.confirmResolver = settle;
      this.set({
        confirm: {
          id: data.requestId,
          summary: req.summary,
          element: req.element,
          deadline: Date.now() + timeoutMs,
        },
      });
    });
  };
}

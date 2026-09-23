import { EventEmitter } from "node:events";

import { ParticipantKind, type Room, RoomEvent, Track } from "livekit-client";

// A stand-in for livekit-client's Room with just the surface DemoSession uses.

type Handler = (...args: never[]) => unknown;

export interface FakeParticipant {
  identity: string;
  kind: ParticipantKind;
  attributes: Record<string, string>;
}

export class FakeRoom extends EventEmitter {
  remoteParticipants = new Map<string, FakeParticipant>();
  canPlaybackAudio = true;
  textHandlers = new Map<string, Handler>();
  rpcHandlers = new Map<string, Handler>();
  sent: { text: string; topic?: string }[] = [];
  rpcCalls: { destinationIdentity: string; method: string; payload: string }[] = [];
  rpcAnswer = '{"element": null}';
  micError: Error | null = null;
  disconnected = false;
  localParticipant = {
    identity: "viewer_s1",
    setMicrophoneEnabled: async () => {
      if (this.micError) throw this.micError;
      return undefined;
    },
    sendText: async (text: string, opts?: { topic?: string }) => {
      this.sent.push({ text, topic: opts?.topic });
      return {};
    },
    performRpc: async (p: { destinationIdentity: string; method: string; payload: string }) => {
      this.rpcCalls.push(p);
      return this.rpcAnswer;
    },
  };

  registerTextStreamHandler(topic: string, cb: Handler) {
    this.textHandlers.set(topic, cb);
  }
  unregisterTextStreamHandler(topic: string) {
    this.textHandlers.delete(topic);
  }
  registerRpcMethod(method: string, cb: Handler) {
    this.rpcHandlers.set(method, cb);
  }
  unregisterRpcMethod(method: string) {
    this.rpcHandlers.delete(method);
  }
  async connect() {}
  async startAudio() {}
  async disconnect() {
    this.disconnected = true;
  }

  asRoom(): Room {
    return this as unknown as Room;
  }

  // --- test drivers ---

  join(p: FakeParticipant) {
    this.remoteParticipants.set(p.identity, p);
    this.emit(RoomEvent.ParticipantConnected, p);
  }

  leave(p: FakeParticipant) {
    this.remoteParticipants.delete(p.identity);
    this.emit(RoomEvent.ParticipantDisconnected, p);
  }

  data(from: FakeParticipant, body: Record<string, unknown>, topic = "ultrademo.events") {
    const payload = new TextEncoder().encode(JSON.stringify({ v: 1, seq: 1, session_id: "s1", ts: 0, ...body }));
    this.emit(RoomEvent.DataReceived, payload, from, undefined, topic);
  }

  screenTrack(from: FakeParticipant) {
    const track = { kind: Track.Kind.Video, attach() {}, detach() {} };
    this.emit(RoomEvent.TrackSubscribed, track, { source: Track.Source.ScreenShare }, from);
    return track;
  }

  /** Feed a text stream to the registered handler, chunk by chunk. */
  async stream(
    topic: string,
    identity: string,
    chunks: string[],
    attributes: Record<string, string>,
    id = "st1",
    cutOff = false,
  ) {
    const handler = this.textHandlers.get(topic) as unknown as (r: unknown, i: { identity: string }) => void;
    const reader = {
      info: { id, attributes, timestamp: 1 },
      async *[Symbol.asyncIterator]() {
        for (const c of chunks) yield c;
        if (cutOff) throw new Error("sender disconnected mid-stream");
      },
    };
    handler(reader, { identity });
    await new Promise((r) => setTimeout(r, 0));
  }
}

export const AGENT: FakeParticipant = {
  identity: "agent-1",
  kind: ParticipantKind.AGENT,
  attributes: { "ultrademo.agent_state": "listening" },
};
export const SANDBOX: FakeParticipant = {
  identity: "sandbox_s1",
  kind: ParticipantKind.STANDARD,
  attributes: { "ultrademo.kind": "sandbox", "ultrademo.screen_meta": '{"viewport":{"w":1440,"h":900}}' },
};
export const STRANGER: FakeParticipant = { identity: "someone", kind: ParticipantKind.STANDARD, attributes: {} };

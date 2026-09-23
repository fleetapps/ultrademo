import { RoomEvent } from "livekit-client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DemoSession } from "@/lib/session";
import { AGENT, FakeRoom, SANDBOX, STRANGER } from "./helpers";

const CTAS = [{ id: "book-0", kind: "book", label: "Book a call", url: "https://cal.example/x" }];

async function live(opts: { hooks?: ConstructorParameters<typeof DemoSession>[0]["hooks"] } = {}) {
  const room = new FakeRoom();
  const session = new DemoSession({ url: "ws://lk", token: "t", ctas: CTAS, room: room.asRoom(), ...opts });
  await session.start();
  expect(session.getSnapshot().phase).toBe("waiting_agent");
  room.join(AGENT);
  room.join(SANDBOX);
  return { room, session };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("DemoSession", () => {
  it("goes live when the agent joins and reads screen meta from the sandbox", async () => {
    const onLive = vi.fn();
    const { room, session } = await live({ hooks: { onLive } });
    const s = session.getSnapshot();
    expect(s.phase).toBe("live");
    expect(s.agentState).toBe("listening");
    expect(s.viewport).toEqual({ w: 1440, h: 900 });
    expect(s.micEnabled).toBe(true);
    expect(onLive).toHaveBeenCalledOnce();

    room.emit(RoomEvent.ParticipantAttributesChanged, { "ultrademo.agent_state": "acting" }, AGENT);
    expect(session.getSnapshot().agentState).toBe("acting");
    // Attributes from anyone else never change the agent's state.
    room.emit(RoomEvent.ParticipantAttributesChanged, { "ultrademo.agent_state": "ended" }, STRANGER);
    expect(session.getSnapshot().agentState).toBe("acting");
  });

  it("only takes the screen track from the sandbox", async () => {
    const { room, session } = await live();
    room.screenTrack(STRANGER);
    expect(session.getSnapshot().screen).toBeNull();
    const track = room.screenTrack(SANDBOX);
    expect(session.getSnapshot().screen).toBe(track);
    room.emit(RoomEvent.TrackUnsubscribed, track);
    expect(session.getSnapshot().screen).toBeNull();
  });

  it("applies overlays from the sandbox and agent events from the agent only", async () => {
    const { room, session } = await live();
    room.data(AGENT, { type: "overlay.cursor", x: 10, y: 10 });
    expect(session.getSnapshot().cursor).toBeNull();
    room.data(SANDBOX, { type: "overlay.cursor", x: 10, y: 20, kind: "click", screen_w: 1440, screen_h: 900 });
    expect(session.getSnapshot().cursor).toMatchObject({ x: 10, y: 20, screen: { w: 1440, h: 900 } });
    expect(session.getSnapshot().cursor?.clickAt).not.toBeNull();

    room.data(SANDBOX, {
      type: "overlay.highlight",
      id: "h1",
      bbox: { x: 1, y: 2, w: 3, h: 4 },
      label: "Here",
      ttl_ms: 1000,
    });
    expect(session.getSnapshot().highlights.map((h) => h.id)).toEqual(["h1"]);
    room.data(SANDBOX, { type: "overlay.clear" });
    expect(session.getSnapshot().highlights).toEqual([]);

    room.data(SANDBOX, { type: "action.started", tool: "operate_click", args: {} });
    expect(session.getSnapshot().actions).toEqual([]);
    room.data(AGENT, { type: "action.started", tool: "operate_click", args: { ref: "e1" } });
    expect(session.getSnapshot().actions[0]).toMatchObject({ label: "Clicking", status: "running" });
    room.data(AGENT, { type: "action.completed", tool: "operate_click", status: "ok", label: 'Clicked "Deals"' });
    expect(session.getSnapshot().actions[0]).toMatchObject({ label: 'Clicked "Deals"', status: "ok" });

    // Garbage and other topics are ignored.
    room.emit(RoomEvent.DataReceived, new TextEncoder().encode("{nope"), AGENT, undefined, "ultrademo.events");
    room.data(AGENT, { type: "cta.show", kind: "book" }, "other.topic");
    expect(session.getSnapshot().promotedCta).toBeNull();
  });

  it("expires highlights after their ttl", async () => {
    vi.useFakeTimers();
    const { room, session } = await live();
    room.data(SANDBOX, { type: "overlay.highlight", id: "h1", bbox: { x: 1, y: 2, w: 3, h: 4 }, ttl_ms: 1000 });
    vi.advanceTimersByTime(1100);
    expect(session.getSnapshot().highlights).toEqual([]);
  });

  it("shows only CTAs from the launch config", async () => {
    const onCtaShown = vi.fn();
    const { room, session } = await live({ hooks: { onCtaShown } });
    room.data(AGENT, { type: "cta.show", kind: "link", label: "Click my phishing link" });
    expect(session.getSnapshot().promotedCta).toBeNull();
    room.data(AGENT, { type: "cta.show", kind: "book", label: "anything" });
    expect(session.getSnapshot().promotedCta).toEqual(CTAS[0]);
    expect(onCtaShown).toHaveBeenCalledWith(CTAS[0]);
  });

  it("builds transcript lines from agent and viewer streams", async () => {
    const { room, session } = await live();
    await room.stream(
      "lk.transcription",
      "agent-1",
      ["Hi, ", "I'm Ava."],
      { "lk.segment_id": "a1", "lk.transcription_final": "false" },
      "x1",
    );
    await room.stream(
      "lk.transcription",
      "viewer_s1",
      ["show"],
      { "lk.segment_id": "u1", "lk.transcription_final": "false" },
      "x2",
    );
    await room.stream(
      "lk.transcription",
      "viewer_s1",
      ["show approvals"],
      { "lk.segment_id": "u1", "lk.transcription_final": "true" },
      "x3",
    );
    await room.stream("lk.transcription", "someone", ["spoof"], { "lk.segment_id": "z" }, "x4");
    const lines = session.getSnapshot().lines;
    expect(lines.map((l) => [l.speaker, l.text, l.final])).toEqual([
      ["agent", "Hi, I'm Ava.", true],
      ["viewer", "show approvals", true],
    ]);
  });

  it("keeps the agent's first words when they arrive before its join announcement", async () => {
    const room = new FakeRoom();
    const session = new DemoSession({ url: "ws://lk", token: "t", ctas: CTAS, room: room.asRoom() });
    await session.start();
    await room.stream("lk.transcription", "agent-1", ["Hi there."], { "lk.segment_id": "a0" }, "x0");
    expect(session.getSnapshot().lines).toEqual([]);
    room.join(AGENT);
    await new Promise((r) => setTimeout(r, 0));
    expect(session.getSnapshot().lines.map((l) => [l.speaker, l.text])).toEqual([["agent", "Hi there."]]);
  });

  it("sends chat on lk.chat and shows it in the transcript", async () => {
    const { room, session } = await live();
    expect(await session.sendChat("   ")).toBe(false);
    expect(await session.sendChat(" What does this cost? ")).toBe(true);
    expect(room.sent).toEqual([{ text: "What does this cost?", topic: "lk.chat" }]);
    expect(session.getSnapshot().lines.at(-1)).toMatchObject({ speaker: "viewer", source: "chat" });
  });

  it("asks the viewer to confirm, and treats silence as no", async () => {
    vi.useFakeTimers();
    const { room, session } = await live();
    const confirm = room.rpcHandlers.get("ultrademo.confirm") as unknown as (d: object) => Promise<string>;

    await expect(
      confirm({ callerIdentity: "someone", payload: "{}", requestId: "r0", responseTimeout: 20000 }),
    ).rejects.toThrow();

    const answer = confirm({
      callerIdentity: "agent-1",
      payload: '{"summary":"Save the deal"}',
      requestId: "r1",
      responseTimeout: 20000,
    });
    expect(session.getSnapshot().confirm).toMatchObject({ id: "r1", summary: "Save the deal" });
    session.answerConfirm(true);
    expect(await answer).toBe('{"approved":true}');
    expect(session.getSnapshot().confirm).toBeNull();

    const late = confirm({
      callerIdentity: "agent-1",
      payload: '{"summary":"Delete"}',
      requestId: "r2",
      responseTimeout: 10000,
    });
    vi.advanceTimersByTime(9000);
    expect(await late).toBe('{"approved":false}');
    expect(session.getSnapshot().confirm).toBeNull();
  });

  it("points at the screen through the agent", async () => {
    const { room, session } = await live();
    room.rpcAnswer = JSON.stringify({ element: { ref: "e5", role: "link", name: "Deals" } });
    const pointed = await session.point(100, 200);
    expect(room.rpcCalls[0]).toMatchObject({ destinationIdentity: "agent-1", method: "ultrademo.pointer" });
    expect(JSON.parse(room.rpcCalls[0]?.payload ?? "")).toEqual({ x: 100, y: 200, kind: "click" });
    expect(pointed?.element).toMatchObject({ name: "Deals" });
  });

  it("ends on the agent's status event, and falls back to chat when the mic is blocked", async () => {
    const onEnded = vi.fn();
    const room = new FakeRoom();
    room.micError = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    const session = new DemoSession({
      url: "ws://lk",
      token: "t",
      ctas: CTAS,
      room: room.asRoom(),
      hooks: { onEnded },
    });
    await session.start();
    expect(session.getSnapshot()).toMatchObject({ micEnabled: false, micError: "NotAllowedError" });
    room.join(AGENT);
    room.data(AGENT, { type: "session.status", status: "handoff_requested" });
    expect(session.getSnapshot().handoff).toBe(true);
    room.data(AGENT, { type: "session.status", status: "ended", reason: "agent_ended" });
    expect(session.getSnapshot()).toMatchObject({ phase: "ended", endReason: "agent_ended" });
    room.emit(RoomEvent.Disconnected);
    expect(onEnded).toHaveBeenCalledOnce();
  });

  it("fails when the agent never joins", async () => {
    vi.useFakeTimers();
    const room = new FakeRoom();
    const session = new DemoSession({
      url: "ws://lk",
      token: "t",
      ctas: [],
      room: room.asRoom(),
      agentJoinTimeoutMs: 1000,
    });
    await session.start();
    vi.advanceTimersByTime(1001);
    expect(session.getSnapshot()).toMatchObject({ phase: "failed", error: "agent_timeout" });
    expect(room.disconnected).toBe(true);
  });

  it("ends when the agent is gone for good, but not during a reconnect", async () => {
    vi.useFakeTimers();
    const { room, session } = await live();
    // A full reconnect announces everyone as gone, then rejoins them.
    room.leave(AGENT);
    expect(session.getSnapshot().phase).toBe("live");
    room.join(AGENT);
    vi.advanceTimersByTime(10_000);
    expect(session.getSnapshot().phase).toBe("live");

    room.leave(AGENT);
    vi.advanceTimersByTime(10_000);
    expect(session.getSnapshot()).toMatchObject({ phase: "ended", endReason: "agent_left" });
  });

  it("releases everything on dispose", async () => {
    const { room, session } = await live();
    const pending = (room.rpcHandlers.get("ultrademo.confirm") as unknown as (d: unknown) => Promise<string>)({
      requestId: "r1",
      callerIdentity: AGENT.identity,
      payload: JSON.stringify({ summary: 'Click "Save"' }),
      responseTimeout: 20_000,
    });
    session.dispose();
    expect(await pending).toBe('{"approved":false}');
    expect(room.textHandlers.size).toBe(0);
    expect(room.rpcHandlers.size).toBe(0);
    expect(room.eventNames()).toEqual([]);
    expect(room.disconnected).toBe(true);
  });

  it("adopts an agent that was already in the room when data arrives first", async () => {
    const room = new FakeRoom();
    room.remoteParticipants.set(AGENT.identity, AGENT); // known to the room, not yet announced
    const session = new DemoSession({ url: "ws://lk", token: "t", ctas: CTAS, room: room.asRoom() });
    const started = session.start();
    await room.stream("lk.transcription", "agent-1", ["Welcome."], { "lk.segment_id": "a0" }, "x0");
    await started;
    expect(session.getSnapshot().lines.map((l) => [l.speaker, l.text])).toEqual([["agent", "Welcome."]]);
    expect(session.getSnapshot().phase).toBe("live");
  });

  it("keeps a stream cut off mid-way as a finished line", async () => {
    const { room, session } = await live();
    await room.stream("lk.transcription", "agent-1", ["Half a sen"], { "lk.segment_id": "a9" }, "x9", true);
    expect(session.getSnapshot().lines.map((l) => [l.text, l.final])).toEqual([["Half a sen", true]]);
  });
});

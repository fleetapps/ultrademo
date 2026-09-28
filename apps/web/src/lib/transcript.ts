// Transcript lines built from LiveKit Agents' transcription text streams.
//
// The agent streams its own speech as deltas within one text stream per segment, and publishes the
// viewer's speech (from STT) as a series of streams that share one `lk.segment_id`, each carrying
// the full text so far. So a line is keyed by segment id, and each stream's accumulated text
// replaces the line's text. This mirrors @livekit/components-core's `setupTextStream`.
//
// The agent only rotates the viewer's segment id when STT reports a final. When it never does for
// an utterance, the viewer's next utterance arrives under the same id; if the agent has spoken in
// between and the text is unrelated, that is a new turn and gets its own line (`<segment>~<n>`)
// instead of rewriting the old bubble above the agent's reply.

export type Speaker = "agent" | "viewer";

export interface Line {
  id: string;
  speaker: Speaker;
  text: string;
  final: boolean;
  source: "voice" | "chat";
  at: number;
}

export const MAX_LINES = 200;

export interface SegmentUpdate {
  id: string;
  speaker: Speaker;
  text: string;
  final: boolean;
  source?: Line["source"];
  at: number;
}

const SPLIT = "~";

function words(text: string): Set<string> {
  return new Set(
    text
      .toLowerCase()
      .split(/[^\p{L}\p{N}']+/u)
      .filter(Boolean),
  );
}

/** Whether `next` is more of the same utterance as `prev` (a longer interim, or a reworded final). */
export function continues(prev: string, next: string): boolean {
  const a = words(prev);
  const b = words(next);
  if (!a.size || !b.size) return true;
  let shared = 0;
  for (const w of a) if (b.has(w)) shared += 1;
  return shared * 2 >= Math.min(a.size, b.size);
}

function append(lines: readonly Line[], line: Line): Line[] {
  const next = [...lines, line];
  return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
}

export function upsertLine(lines: readonly Line[], u: SegmentUpdate): Line[] {
  const i = lines.findLastIndex((l) => l.id === u.id || l.id.startsWith(u.id + SPLIT));
  const prev = lines[i];
  if (!prev) {
    if (!u.text.trim()) return lines as Line[];
    return append(lines, { ...u, source: u.source ?? "voice" });
  }
  if (
    !prev.final &&
    u.text.trim() &&
    lines.slice(i + 1).some((l) => l.speaker !== prev.speaker) &&
    !continues(prev.text, u.text)
  ) {
    // A reused segment id after the other side spoke: close the old line and start a new turn.
    const n = prev.id === u.id ? 2 : Number(prev.id.slice(u.id.length + 1)) + 1;
    const closed = [...lines];
    closed[i] = { ...prev, final: true };
    return append(closed, { ...u, id: `${u.id}${SPLIT}${n}`, source: u.source ?? "voice" });
  }
  // A final line never goes back to interim, and never loses its text to an empty update.
  const line: Line = {
    ...prev,
    text: u.text.trim() ? u.text : prev.text,
    final: prev.final || u.final,
  };
  if (line.text === prev.text && line.final === prev.final) return lines as Line[];
  const next = [...lines];
  next[i] = line;
  return next;
}

/** The newest agent line, for the caption bar. */
export function latestAgentLine(lines: readonly Line[]): Line | undefined {
  return lines.findLast((l) => l.speaker === "agent");
}

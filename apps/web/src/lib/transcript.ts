// Transcript lines built from LiveKit Agents' transcription text streams.
//
// The agent streams its own speech as deltas within one text stream per segment, and publishes the
// viewer's speech (from STT) as a series of streams that share one `lk.segment_id`, each carrying
// the full text so far. So a line is keyed by segment id, and each stream's accumulated text
// replaces the line's text. This mirrors @livekit/components-core's `setupTextStream`.

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

export function upsertLine(lines: readonly Line[], u: SegmentUpdate): Line[] {
  const i = lines.findIndex((l) => l.id === u.id);
  const prev = lines[i];
  if (!prev) {
    if (!u.text.trim()) return lines as Line[];
    const next = [...lines, { ...u, source: u.source ?? "voice" }];
    return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
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

// The silent reply. When the agent has nothing to say it answers with exactly
// `[IGNORE]`, and delivery sends nothing (`delivery.IGNORE_MARKER` in
// core/joshua_core/delivery.py; the transcript calls it `SILENT_REPLY`). The
// viewer must not show it either: not in the history, and not while the
// answer streams. This is the one place that knows the marker. The history
// route of core leaves the row out already; this is the same rule for the
// live stream, and a second check for a history page.

export const SILENT_REPLY = "[IGNORE]";

/** True when `text` is the silent reply, with any blanks around it. */
export function isSilentReply(text: string): boolean {
  return text.trim() === SILENT_REPLY;
}

/**
 * True while `text` could still become the silent reply: it is the marker or
 * a start of it (`[IGN`), with any blanks around it. A streaming answer that
 * is a start of the marker is not shown yet, so `[IGN` never flashes.
 */
export function maybeSilentReply(text: string): boolean {
  const t = text.trim();
  return t !== "" && SILENT_REPLY.startsWith(t);
}

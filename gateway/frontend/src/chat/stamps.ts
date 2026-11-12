// When the list shows a time line. One line stands before a message when it
// is the first one, when the day changed, or when more than STAMP_GAP_MS
// passed since the message before it. So a run of messages a few minutes
// apart shares one line.

import type { ChatMessage } from "./store";

export const STAMP_GAP_MS = 5 * 60 * 1000;

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

export function needsStamp(
  prev: ChatMessage | undefined,
  cur: ChatMessage,
  gapMs: number = STAMP_GAP_MS,
): boolean {
  if (!prev) return true;
  const a = new Date(prev.ts).getTime();
  const b = new Date(cur.ts).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return false;
  if (!sameDay(new Date(a), new Date(b))) return true;
  return b - a > gapMs;
}

export function formatStamp(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (sameDay(d, now)) return time;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(d, yesterday)) return `Yesterday, ${time}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  const day = d.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
  return `${day}, ${time}`;
}

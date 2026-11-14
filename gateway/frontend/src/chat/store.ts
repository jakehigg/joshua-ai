// The conversation as this screen shows it, in memory only.
//
// Joshua's transcript is the history. The browser stores none of it. A
// server row has `serverId`. A local message (no `serverId`) is one that
// this screen sent and that the transcript does not show yet: the person
// message of a turn and its Joshua answer while the answer streams, or a
// person message that the backend refused.

import type { FileRef, Held, HistoryMessage, HistoryPage, Pending } from "../api/client";
import { isSilentReply } from "./silent";

export type Role = "person" | "joshua" | "system";

// ok: complete. pending: the answer is arriving. failed: Joshua sent an
// error frame, or the transcript row is failed. lost: the connection broke
// before the end.
export type MessageState = "ok" | "pending" | "failed" | "lost";

export interface ChatMessage {
  id: string;
  // The transcript row id. Only server rows have it.
  serverId?: number;
  role: Role;
  text: string;
  // ISO time: when the message was sent, or when an answer started.
  ts: string;
  state: MessageState;
  // failed or lost: what went wrong, for the person.
  error?: string;
  // On a person message: the backend refused the turn (a non-2xx answer).
  refused?: string;
  // On a local Joshua message: the person text to send again on Retry.
  retryText?: string;
  // On a local Joshua message: the file to send again on Retry.
  retryFile?: File;
  // On a local Joshua message: the id of the person message it answers.
  forId?: string;
  // Retry was used; the button is not shown again.
  retried?: boolean;
  turnId?: string;
  tools?: string[];
  // On a person message: it was sent with Brief on (a spoken turn).
  spoken?: boolean;
  // On a local person message: the name of the file sent with it, for the chip.
  fileName?: string;
  // On a local person message: the file itself, to send again on Retry.
  file?: File;
  // On a server person message: the files that came with it.
  attachments?: FileRef[];
  // On a local Joshua message: the turn waits for another turn of the
  // conversation to end.
  waiting?: boolean;
}

// One held message on screen: a message written while a turn ran, which the
// server holds for the next turn. `sending` is one the server has not
// answered for yet.
export interface HeldMessage extends Held {
  sending?: boolean;
}

// The messages on screen and the cursor of the page before them.
export interface View {
  messages: ChatMessage[];
  nextBefore: number | null;
  pending: Pending | null;
}

export const EMPTY_VIEW: View = { messages: [], nextBefore: null, pending: null };

// A reply that says nothing is not a message of the conversation (silent.ts).
function listed(rows: HistoryMessage[]): HistoryMessage[] {
  return rows.filter((m) => !(m.role === "joshua" && m.status !== "failed" && isSilentReply(m.text)));
}

export function serverKey(id: number): string {
  return `s${id}`;
}

export function fromServer(m: HistoryMessage): ChatMessage {
  return {
    id: serverKey(m.id),
    serverId: m.id,
    role: m.role,
    text: m.text,
    ts: m.at,
    state: m.status === "failed" ? "failed" : "ok",
    ...(m.turnId ? { turnId: m.turnId } : {}),
    ...(m.tools ? { tools: m.tools } : {}),
    ...(m.spoken ? { spoken: true } : {}),
    ...(m.attachments ? { attachments: m.attachments } : {}),
  };
}

// The local messages a refresh keeps: an answer that is still pending, the
// person message it answers, and a refused person message that was not
// retried (the transcript never has it).
export function keepLocal(messages: ChatMessage[]): ChatMessage[] {
  const answering = new Set(
    messages
      .filter((m) => m.serverId === undefined && m.state === "pending" && m.forId)
      .map((m) => m.forId as string),
  );
  return messages.filter(
    (m) =>
      m.serverId === undefined &&
      (m.state === "pending" || answering.has(m.id) || (!!m.refused && !m.retried)),
  );
}

function unique(messages: ChatMessage[]): ChatMessage[] {
  const seen = new Set<number>();
  return messages.filter((m) => {
    if (m.serverId === undefined) return true;
    if (seen.has(m.serverId)) return false;
    seen.add(m.serverId);
    return true;
  });
}

// Puts the newest page on screen. The server list replaces the server rows.
// Older rows that "Load earlier" added stay when the new page joins them
// (an old row is at or after the oldest row of the new page); then the old
// cursor stays too. The local messages that `keepLocal` names stay at the end.
export function reconcile(view: View, page: HistoryPage): View {
  const fresh = unique(listed(page.messages).map(fromServer));
  const local = keepLocal(view.messages);
  if (fresh.length === 0 || page.nextBefore === null) {
    return { messages: [...fresh, ...local], nextBefore: page.nextBefore, pending: page.pending };
  }
  const oldest = Math.min(...fresh.map((m) => m.serverId as number));
  const shown = view.messages.filter((m) => m.serverId !== undefined);
  const joins = shown.some((m) => (m.serverId as number) >= oldest);
  const older = joins ? shown.filter((m) => (m.serverId as number) < oldest) : [];
  return {
    messages: [...older, ...fresh, ...local],
    nextBefore: older.length > 0 ? view.nextBefore : page.nextBefore,
    pending: page.pending,
  };
}

// Adds the page before the oldest row on screen. A row already on screen
// is not added again.
export function prependOlder(view: View, page: HistoryPage): View {
  const present = new Set(view.messages.map((m) => m.serverId).filter((id) => id !== undefined));
  const older = unique(listed(page.messages).map(fromServer)).filter(
    (m) => !present.has(m.serverId as number),
  );
  return { messages: [...older, ...view.messages], nextBefore: page.nextBefore, pending: view.pending };
}

export function appendMessage(messages: ChatMessage[], ...added: ChatMessage[]): ChatMessage[] {
  return [...messages, ...added];
}

export function updateMessage(
  messages: ChatMessage[],
  id: string,
  patch: Partial<ChatMessage> | ((m: ChatMessage) => Partial<ChatMessage>),
): ChatMessage[] {
  return messages.map((m) =>
    m.id === id ? { ...m, ...(typeof patch === "function" ? patch(m) : patch) } : m,
  );
}

let counter = 0;

// A local message id. crypto.randomUUID needs a secure context, and this
// app can run on plain HTTP on a private network, so the id does not use it.
export function newId(): string {
  counter += 1;
  return `l${Date.now().toString(36)}-${counter.toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

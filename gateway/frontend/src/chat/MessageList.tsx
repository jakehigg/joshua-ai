// The scrolling list of messages. While the reader is at the bottom, a new
// message or new text scrolls into view. When the reader has scrolled up,
// the list stays put and a "new" button appears instead — except for a
// message the reader sends: `sent` rises then, and the list goes to the
// bottom, so they see their own message and the answer to it. "Load earlier"
// at the top adds the page before; the rows on screen do not move. A time
// line stands before the first message of a run (see stamps.ts), and in a
// shared workspace the name of the writer stands above the first message of
// their run: the person (`who`), or Joshua.
//
// In a shared workspace the list also says which side a message takes and
// which colour: the messages of the person who is signed in (`me`, their
// name) stay at the right, and every other person's go to the left, each in
// the colour of its writer (tone.ts). A personal thread passes no `me` and no
// `shared`, and keeps the layout it has.
//
// `pending` is a turn that Joshua works on and this screen does not stream (a
// message sent from another device): the list ends with the same working line,
// and with the message of the person when the transcript does not hold it yet.
//
// `held` are the messages written while a turn runs. They end the list, each
// marked as waiting, with a button that takes one back before it goes.
//
// While the newest row of the thread is on screen, the list says so with
// `onRead`, so the screen can mark the thread read up to that row.

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Pending } from "../api/client";
import type { ChatMessage, HeldMessage } from "./store";
import { Held, Message, Working } from "./Message";
import { formatStamp, needsStamp } from "./stamps";
import { toneOf } from "./tone";
import type { LoadStatus } from "./useHistory";

// Pixels from the bottom that still count as "at the bottom".
const NEAR_BOTTOM_PX = 48;

// The signed-in person wrote this row. A row with no `who` is theirs: a
// personal thread has none, and neither has a message this screen just sent.
function mine(message: ChatMessage, me?: string): boolean {
  return !message.who || message.who === me;
}

// A bubble at the left of a shared thread says who wrote it, above the first
// message of the run and not above the rest: the name of the other person, or
// Joshua. The person's own messages stand at the right and carry no name.
function namesWho(
  before: ChatMessage | undefined,
  message: ChatMessage,
  me: string | undefined,
  shared: boolean,
): boolean {
  if (message.role === "joshua") return shared && before?.role !== "joshua";
  if (message.role !== "person" || mine(message, me)) return false;
  return before?.role !== "person" || before.who !== message.who;
}

// The colour of a bubble in a shared thread: the writer's own, and none at
// all in a personal thread. A message this screen just sent carries no `who`
// yet, so the person's own name stands in and the bubble keeps its colour
// when the row of the transcript takes its place.
function toneFor(message: ChatMessage, me: string | undefined, shared: boolean): string {
  if (!shared || message.role !== "person") return "";
  const who = message.who || me;
  return who ? toneOf(who) : "";
}

function reducedMotion(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

interface Props {
  messages: ChatMessage[];
  canRetry: boolean;
  onRetry: (message: ChatMessage) => void;
  status?: LoadStatus;
  error?: string | null;
  onReload?: () => void;
  hasEarlier?: boolean;
  loadingEarlier?: boolean;
  onLoadEarlier?: () => void;
  pending?: Pending | null;
  held?: HeldMessage[];
  onDropHeld?: (id: string) => void;
  /** The name of the signed-in person: their own messages take the right. */
  me?: string;
  /** The thread of a shared workspace: several people write in it. */
  shared?: boolean;
  /**
   * The newest row of the thread is on screen: the thread is read up to that
   * row id. It is called again for each newer row that arrives.
   */
  onRead?: (serverId: number) => void;
  /** How many messages this screen has sent: a send goes to the bottom. */
  sent?: number;
}

export function MessageList({
  messages,
  canRetry,
  onRetry,
  status = "ready",
  error = null,
  onReload,
  hasEarlier = false,
  loadingEarlier = false,
  onLoadEarlier,
  pending = null,
  held = [],
  onDropHeld,
  me,
  shared = false,
  onRead,
  sent = 0,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const [unseen, setUnseen] = useState(false);
  // The scroll height and position before "Load earlier", to restore after.
  const anchor = useRef<{ height: number; top: number } | null>(null);

  // The newest row the server has: how far the thread is read when it shows.
  // A local message has no id yet, and an older page adds none above it.
  const newest = messages.reduce((id, m) => ((m.serverId ?? 0) > id ? (m.serverId as number) : id), 0);
  const onReadRef = useRef(onRead);
  onReadRef.current = onRead;
  // The thread is read up to its newest row, unless the tab is hidden: a row
  // that arrives behind another tab is not on screen.
  const readHere = useCallback(() => {
    if (newest > 0 && document.visibilityState !== "hidden") onReadRef.current?.(newest);
  }, [newest]);

  const toBottom = useCallback((smooth: boolean) => {
    const el = ref.current;
    if (!el) return;
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
    atBottom.current = true;
    setUnseen(false);
  }, []);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
    if (atBottom.current) {
      setUnseen(false);
      readHere();
    }
  };

  // The newest content: the last message and its text length. A page added
  // at the top does not change it.
  const last = messages[messages.length - 1];
  const signature = `${last?.id ?? ""}:${last?.text.length ?? 0}:${last?.state ?? ""}:${pending?.turnId ?? ""}:${held.length}`;
  // The message of a queued turn is not in the transcript yet.
  const ghost: ChatMessage | null =
    pending && pending.text && !messages.some((m) => m.turnId === pending.turnId)
      ? { id: `pending-${pending.turnId}`, role: "person", text: pending.text, ts: pending.since, state: "ok" }
      : null;

  const loadEarlier = () => {
    const el = ref.current;
    if (el) anchor.current = { height: el.scrollHeight, top: el.scrollTop };
    onLoadEarlier?.();
  };

  // After the page before is added, keep the same rows in view.
  const first = messages[0]?.id;
  useLayoutEffect(() => {
    const el = ref.current;
    const a = anchor.current;
    if (!el || !a) return;
    anchor.current = null;
    el.scrollTop = a.top + (el.scrollHeight - a.height);
  }, [first]);

  // A load that failed or added nothing leaves no anchor behind.
  useLayoutEffect(() => {
    if (!loadingEarlier) anchor.current = null;
  }, [loadingEarlier]);

  useLayoutEffect(() => {
    if (atBottom.current) {
      toBottom(false);
      readHere();
    } else setUnseen(true);
  }, [signature, toBottom, readHere]);

  // A send goes to the bottom, wherever the reader stood. It runs after the
  // effect above, so the "new" button of that one does not come up instead.
  useLayoutEffect(() => {
    if (sent === 0) return;
    toBottom(false);
    readHere();
  }, [sent, toBottom, readHere]);

  // A tab that comes back shows the rows that arrived while it was hidden.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && atBottom.current) readHere();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [readHere]);

  // A growing composer or a rotated phone changes the height; stay at the bottom.
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (atBottom.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="list-wrap">
      <div
        ref={ref}
        className="list"
        role="log"
        aria-live="polite"
        aria-label="Conversation with Joshua"
        onScroll={onScroll}
      >
        <div className="column">
          {hasEarlier && (
            <button
              type="button"
              className="load-earlier"
              onClick={loadEarlier}
              disabled={loadingEarlier}
            >
              {loadingEarlier ? "Loading…" : "Load earlier"}
            </button>
          )}
          {status === "loading" && (
            <p className="history-state" role="status">
              Loading…
            </p>
          )}
          {status === "error" && (
            <div className="msg-error history-state" role="alert">
              <span>The conversation could not be loaded: {error}</span>
              {onReload && (
                <button type="button" className="retry" onClick={onReload}>
                  Retry
                </button>
              )}
            </div>
          )}
          {status === "ready" && messages.length === 0 && (
            <p className="empty">Ask Joshua anything. Answers can take a while when Joshua uses tools.</p>
          )}
          {messages.map((m, i) => (
            <Fragment key={m.id}>
              {needsStamp(messages[i - 1], m) && (
                <div className="stamp">
                  <time dateTime={m.ts}>{formatStamp(m.ts)}</time>
                </div>
              )}
              <Message
                message={m}
                canRetry={canRetry}
                onRetry={onRetry}
                showWho={namesWho(messages[i - 1], m, me, shared)}
                mine={mine(m, me)}
                tone={toneFor(m, me, shared)}
              />
            </Fragment>
          ))}
          {ghost && (
            <Message
              message={ghost}
              canRetry={false}
              onRetry={onRetry}
              tone={toneFor(ghost, me, shared)}
            />
          )}
          {pending && (
            <div className="reading pending-turn">
              <Working since={pending.since} waiting={pending.state === "queued"} />
            </div>
          )}
          {held.map((m) => (
            <Held key={m.id} message={m} onRemove={(id) => onDropHeld?.(id)} />
          ))}
        </div>
      </div>
      {unseen && (
        <button
          type="button"
          className="new-below"
          onClick={() => {
            toBottom(true);
            readHere();
          }}
          aria-label="Scroll to the newest message"
        >
          ↓ new
        </button>
      )}
    </div>
  );
}

// The scrolling list of messages. While the reader is at the bottom, a new
// message or new text scrolls into view. When the reader has scrolled up,
// the list stays put and a "new" button appears instead. "Load earlier"
// at the top adds the page before; the rows on screen do not move. A time
// line stands before the first message of a run (see stamps.ts).
//
// `pending` is a turn that Joshua works on and this screen does not stream (a
// message sent from another device): the list ends with the same working line,
// and with the message of the person when the transcript does not hold it yet.
//
// `held` are the messages written while a turn runs. They end the list, each
// marked as waiting, with a button that takes one back before it goes.

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Pending } from "../api/client";
import type { ChatMessage, HeldMessage } from "./store";
import { Held, Message, Working } from "./Message";
import { formatStamp, needsStamp } from "./stamps";
import type { LoadStatus } from "./useHistory";

// Pixels from the bottom that still count as "at the bottom".
const NEAR_BOTTOM_PX = 48;

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
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const [unseen, setUnseen] = useState(false);
  // The scroll height and position before "Load earlier", to restore after.
  const anchor = useRef<{ height: number; top: number } | null>(null);

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
    if (atBottom.current) setUnseen(false);
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
    if (atBottom.current) toBottom(false);
    else setUnseen(true);
  }, [signature, toBottom]);

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
              <Message message={m} canRetry={canRetry} onRetry={onRetry} />
            </Fragment>
          ))}
          {ghost && <Message message={ghost} canRetry={false} onRetry={onRetry} />}
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
        <button type="button" className="new-below" onClick={() => toBottom(true)} aria-label="Scroll to the newest message">
          ↓ new
        </button>
      )}
    </div>
  );
}

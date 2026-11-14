// One message. The person's text is plain, in a bubble on the right. Joshua's
// text is Markdown, full width, with no bubble. A system row (a task prompt
// or an event) is one small, muted line at the left (EventLine, events.ts):
// an icon, the first sentence, a Question badge and its text for a question,
// and a chip for each link. It opens to the full text as Markdown, without the
// sentences that were written for Joshua, and Show raw gives the exact text. A failed
// transcript row shows its text in the error style. The time lines belong to
// the list (stamps.ts), not to a message.
//
// A Joshua answer can end with an `artifact` block, the path of a document
// the turn wrote. The block is never printed as code: the answer shows
// without it, and a card under the answer shows the document
// (ArtifactCard.tsx). An old answer can carry a `speech` block, which is
// taken out too and shows nowhere. Both blocks are part of the stored answer
// text, so a reloaded history row goes through this same code path.
// A person message that carried a photo or a PDF shows it above its bubble, at
// the right like the bubble, with no frame of its own (a file with no caption
// has no bubble at all): a photo as a thumbnail that opens the full image, a PDF as a chip with its name. A
// message that this screen just sent shows the file it holds; a message from
// the history shows the files the transcript row names, from the viewer's
// files route. A turn that waits for another turn of the conversation says so.
// A message written while a turn runs (`Held`) is a person bubble that says
// it waits, with a button to take it back.
//
// In the thread of a shared workspace a person row carries `who`, the name of
// the person who wrote it. A small line above the bubble names them, once for
// a run of messages from the same person (`showWho`, which the list decides).

import { memo, useEffect, useMemo, useState } from "react";
import type { FileRef } from "../api/client";
import type { ChatMessage, HeldMessage } from "./store";
import { useDocumentPanel } from "./DocumentPanel";
import { ARTIFACT_INFO, SPEECH_INFO, splitFence } from "./blocks";
import { ArtifactCard } from "./ArtifactCard";
import { Markdown } from "./Markdown";
import { cleanEvent, eventLinks, headline, isQuestion, questionPreview } from "./events";

export const ELAPSED_FROM_S = 5;
export const SLOW_FROM_S = 30;

export function Working({ since, waiting = false }: { since: string; waiting?: boolean }) {
  const start = new Date(since).getTime();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const seconds = Number.isNaN(start) ? 0 : Math.max(0, Math.floor((now - start) / 1000));
  return (
    <div className="working" role="status">
      <span className="dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <span>
        {waiting
          ? "Waiting for Joshua to finish another turn…"
          : seconds >= SLOW_FROM_S
            ? "Joshua is still working, tools can take a while"
            : "Joshua is working…"}
      </span>
      {seconds >= ELAPSED_FROM_S && <span className="elapsed">{seconds} s</span>}
    </div>
  );
}

// A message written while a turn ran. It waits in the hold of the thread,
// on the server, and goes with every other waiting message as the next
// turn. Remove takes it back before it goes.
export function Held({
  message,
  onRemove,
}: {
  message: HeldMessage;
  onRemove: (id: string) => void;
}) {
  return (
    <article className="msg msg-person msg-held">
      <div className="bubble">{message.text}</div>
      <div className="held-note">
        <span>Waiting to go with the next turn</span>
        <button
          type="button"
          className="retry"
          onClick={() => onRemove(message.id)}
          aria-label={`Remove the waiting message: ${message.text}`}
        >
          Remove
        </button>
      </div>
    </article>
  );
}

function FileChip({ name, url }: { name: string; url?: string }) {
  const inner = (
    <>
      <span aria-hidden="true">📎</span>
      <span className="file-name">{name}</span>
    </>
  );
  return url ? (
    <a className="file-chip sent" href={url} target="_blank" rel="noopener noreferrer">
      {inner}
    </a>
  ) : (
    <span className="file-chip sent">{inner}</span>
  );
}

// A photo as a thumbnail. A tap shows it full size in the panel of the chat;
// outside a chat screen the link opens the image in a new tab.
function Attachment({ file }: { file: FileRef }) {
  const panel = useDocumentPanel();
  if (file.mime.startsWith("image/") && file.url) {
    const url = file.url;
    return (
      <a
        className="thumb-link"
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => {
          if (!panel.live) return;
          e.preventDefault();
          panel.open({ type: "image", image: { url, title: file.name } });
        }}
      >
        <img className="thumb" src={url} alt={file.name} loading="lazy" />
      </a>
    );
  }
  return <FileChip name={file.name} url={file.url} />;
}

// The file this screen just sent. A photo shows from the bytes in memory.
function LocalFile({ file }: { file: File }) {
  const url = useMemo(
    () =>
      file.type.startsWith("image/") && typeof URL.createObjectURL === "function"
        ? URL.createObjectURL(file)
        : null,
    [file],
  );
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  if (!url) return <FileChip name={file.name} />;
  return <img className="thumb" src={url} alt={file.name} />;
}

// The text that a file with no caption of its own gets (shared/attachments.py).
const NO_CAPTION = "(file attached — no caption)";

interface Props {
  message: ChatMessage;
  canRetry: boolean;
  onRetry: (message: ChatMessage) => void;
  /** Name the person above the bubble: the first message of their run. */
  showWho?: boolean;
}

function EventLine({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const question = isQuestion(text);
  const preview = question ? questionPreview(text) : "";
  const links = eventLinks(text);
  const cleaned = useMemo(() => cleanEvent(text), [text]);
  return (
    <div className="event-line">
      <div className="event-row">
        <button
          type="button"
          className="event-head"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <span className="event-icon" aria-hidden="true">
            {question ? "❓" : "🔔"}
          </span>
          {question && <span className="event-badge">Question</span>}
          <span className="event-title">{headline(text) || "Event"}</span>
          {preview && <span className="event-preview">{preview}</span>}
        </button>
        {links.map((link) => (
          <a
            key={link.href}
            className="event-chip"
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            title={link.href}
          >
            {link.label}
          </a>
        ))}
      </div>
      {open && (
        <div className="event-body">
          {raw ? <pre className="event-raw">{text}</pre> : <Markdown text={cleaned} />}
          <button type="button" className="event-raw-toggle" onClick={() => setRaw((r) => !r)}>
            {raw ? "Show formatted" : "Show raw"}
          </button>
        </div>
      )}
    </div>
  );
}

export const Message = memo(function Message({ message, canRetry, onRetry, showWho }: Props) {
  const isPerson = message.role === "person";
  const pending = message.state === "pending";
  const answer = useMemo(() => {
    if (message.role !== "joshua") return null;
    const spoken = splitFence(message.text, SPEECH_INFO, pending);
    const artifact = splitFence(spoken.display, ARTIFACT_INFO, pending);
    return { display: artifact.display, artifact: artifact.content };
  }, [message.role, message.text, pending]);
  if (message.role === "system") {
    return (
      <article className="msg msg-system">
        <EventLine text={message.text} />
      </article>
    );
  }
  // A failed row from the transcript: its text is the error.
  const failedRow = message.serverId !== undefined && message.state === "failed";
  const hasFile = !!message.file || !!message.fileName || !!message.attachments?.length;
  const retryButton = !message.retried && (
    <button
      type="button"
      className="retry"
      onClick={() => onRetry(message)}
      disabled={!canRetry}
      aria-label="Retry: send the same text again"
    >
      Retry
    </button>
  );

  return (
    <article className={`msg ${isPerson ? "msg-person" : "msg-joshua"}`}>
      {isPerson ? (
        <>
          {showWho && message.who && <div className="msg-who">{message.who}</div>}
          {hasFile && (
            <div className="msg-media">
              {message.file ? (
                <LocalFile file={message.file} />
              ) : (
                message.fileName && <FileChip name={message.fileName} />
              )}
              {message.attachments?.map((file) => (
                <Attachment key={file.url ?? file.name} file={file} />
              ))}
            </div>
          )}
          {(!hasFile || message.text !== NO_CAPTION) && message.text && (
            <div className="bubble">{message.text}</div>
          )}
        </>
      ) : (
        <div className="reading">
          {failedRow ? (
            <div className="msg-error failed-row">{message.text}</div>
          ) : (
            <>
              {answer?.display && <Markdown text={answer.display} />}
              {answer?.artifact && <ArtifactCard content={answer.artifact} />}
            </>
          )}
          {pending && <Working since={message.ts} waiting={message.waiting} />}
        </div>
      )}
      {isPerson && message.refused && (
        <div className="msg-error" role="alert">
          <span>{message.refused}</span>
          {retryButton}
        </div>
      )}
      {!isPerson && !failedRow && (message.state === "failed" || message.state === "lost") && (
        <div className="msg-error" role="alert">
          <span>{message.error || "the connection was lost"}</span>
          {message.retryText && retryButton}
        </div>
      )}
    </article>
  );
});

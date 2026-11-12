// One message. The person's text is plain, in a bubble on the right. Joshua's
// text is Markdown, full width, with no bubble. A system row (a task prompt
// or an event) is one small, muted, centered line of plain text. A failed
// transcript row shows its text in the error style. The time lines belong to
// the list (stamps.ts), not to a message.
//
// A Joshua answer can end with an `artifact` block, the path of a document
// the turn wrote. The block is never printed as code: the answer shows
// without it, and a card under the answer shows the document
// (ArtifactCard.tsx). An old answer can carry a `speech` block, which is
// taken out too and shows nowhere. Both blocks are part of the stored answer
// text, so a reloaded history row goes through this same code path.
// A person message that carried a photo or a PDF shows a chip with its name.

import { memo, useEffect, useMemo, useState } from "react";
import type { ChatMessage } from "./store";
import { ARTIFACT_INFO, SPEECH_INFO, splitFence } from "./blocks";
import { ArtifactCard } from "./ArtifactCard";
import { Markdown } from "./Markdown";

export const ELAPSED_FROM_S = 5;
export const SLOW_FROM_S = 30;

function Working({ since }: { since: string }) {
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
        {seconds >= SLOW_FROM_S ? "Joshua is still working, tools can take a while" : "Joshua is working…"}
      </span>
      {seconds >= ELAPSED_FROM_S && <span className="elapsed">{seconds} s</span>}
    </div>
  );
}

interface Props {
  message: ChatMessage;
  canRetry: boolean;
  onRetry: (message: ChatMessage) => void;
}

export const Message = memo(function Message({ message, canRetry, onRetry }: Props) {
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
        <p className="system-line">{message.text}</p>
      </article>
    );
  }
  // A failed row from the transcript: its text is the error.
  const failedRow = message.serverId !== undefined && message.state === "failed";
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
        <div className="bubble">
          {message.fileName && (
            <span className="file-chip sent">
              <span aria-hidden="true">📎</span>
              <span className="file-name">{message.fileName}</span>
            </span>
          )}
          {message.text}
        </div>
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
          {pending && <Working since={message.ts} />}
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

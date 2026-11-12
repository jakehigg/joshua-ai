// The streaming state machine for one turn.
//
// send() appends the person's message and an empty Joshua message in the
// `pending` state, then streams the answer into it:
//   delta          -> append the text
//   done           -> set the text to done.text; state ok
//   error frame    -> state failed, with the message; partial text is kept
//   non-2xx answer -> remove the empty Joshua message; the person message
//                     carries the refusal, and onRefused shows a toast
//   lost stream    -> state lost; partial text is kept
// A turn can carry one photo or one PDF. The file goes with the request as
// multipart; the person message keeps its name for the chip, and the file
// itself, so Retry sends the same message again.
// One turn runs at a time. After a `done`, onDone runs, once the turn is
// no longer running; the chat page refreshes the history then. When the
// finished answer carries an `artifact` fence, onArtifact gets the document
// path, so the panel can reload a frame that has that document open
// (DocumentPanel.tsx): the refine loop inside one turn. The path comes from
// the answer and not from the stream, because the `done` frame's `tools` is
// a de-duped list of tool names and never says which document was written.
// `workspace` is passed through to the turn body: the turn runs in that
// workspace, and without it in the main thread.

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ApiError, ConnectionLost, postTurn } from "../api/client";
import { parseArtifact } from "./ArtifactCard";
import { ARTIFACT_INFO, splitFence } from "./blocks";
import { appendMessage, newId, updateMessage, type ChatMessage } from "./store";

type SetMessages = Dispatch<SetStateAction<ChatMessage[]>>;

// The document path of the finished answer's `artifact` fence, or null when
// it has none, or one that is not a document path.
function writtenPath(text: string): string | null {
  const { content } = splitFence(text, ARTIFACT_INFO);
  if (!content) return null;
  return parseArtifact(content)?.path ?? null;
}

export interface Turn {
  running: boolean;
  // The running state now, also between a change and the next render.
  isRunning: () => boolean;
  send: (text: string, file?: File | null) => boolean;
  retry: (message: ChatMessage) => void;
}

export function useTurn(
  person: string,
  setMessages: SetMessages,
  onRefused?: (message: string) => void,
  onDone?: () => void,
  onArtifact?: (path: string) => void,
  workspace?: string,
): Turn {
  const [running, setRunning] = useState(false);
  const runningRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const onRefusedRef = useRef(onRefused);
  onRefusedRef.current = onRefused;
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const onArtifactRef = useRef(onArtifact);
  onArtifactRef.current = onArtifact;

  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  const run = useCallback(
    async (personMsg: ChatMessage, replyId: string) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      let completed = false;
      // The document the finished answer says it wrote, if it says one.
      let written: string | null = null;
      const patch = (p: Partial<ChatMessage> | ((m: ChatMessage) => Partial<ChatMessage>)) =>
        setMessages((list) => updateMessage(list, replyId, p));
      try {
        await postTurn(
          person,
          personMsg.text,
          (frame) => {
            switch (frame.type) {
              case "delta":
                patch((m) => ({ text: m.text + frame.text }));
                break;
              case "done":
                completed = true;
                patch({ text: frame.text, state: "ok", ...(frame.turnId ? { turnId: frame.turnId } : {}) });
                written = writtenPath(frame.text);
                break;
              case "error":
                patch({ state: "failed", error: frame.message });
                break;
              default:
                break;
            }
          },
          controller.signal,
          personMsg.file,
          workspace,
        );
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError) {
          setMessages((list) =>
            updateMessage(
              list.filter((m) => m.id !== replyId),
              personMsg.id,
              { refused: err.message },
            ),
          );
          onRefusedRef.current?.(err.message);
        } else {
          const message = err instanceof ConnectionLost ? err.message : "the connection was lost";
          patch({ state: "lost", error: message });
        }
      } finally {
        if (controllerRef.current === controller) controllerRef.current = null;
        if (!controller.signal.aborted) {
          runningRef.current = false;
          setRunning(false);
          if (completed) {
            onDoneRef.current?.();
            if (written) onArtifactRef.current?.(written);
          }
        }
      }
    },
    [person, workspace, setMessages],
  );

  const send = useCallback(
    (text: string, file?: File | null): boolean => {
      if (runningRef.current || (!text.trim() && !file)) return false;
      runningRef.current = true;
      setRunning(true);
      const now = new Date().toISOString();
      const personMsg: ChatMessage = {
        id: newId(),
        role: "person",
        text,
        ts: now,
        state: "ok",
        ...(file ? { fileName: file.name, file } : {}),
      };
      const reply: ChatMessage = {
        id: newId(),
        role: "joshua",
        text: "",
        ts: now,
        state: "pending",
        retryText: text,
        ...(file ? { retryFile: file } : {}),
        forId: personMsg.id,
      };
      setMessages((list) => appendMessage(list, personMsg, reply));
      void run(personMsg, reply.id);
      return true;
    },
    [run, setMessages],
  );

  // A refused person message is removed and sent again at the end. A failed
  // or lost answer stays on screen with its partial text; the same person
  // text, with the same file, is sent again as a new turn.
  const retry = useCallback(
    (message: ChatMessage) => {
      if (runningRef.current) return;
      const person = message.role === "person";
      const text = person ? message.text : message.retryText;
      const file = person ? message.file : message.retryFile;
      if (!text && !file) return;
      setMessages((list) =>
        person
          ? list.filter((m) => m.id !== message.id)
          : updateMessage(list, message.id, { retried: true }),
      );
      send(text ?? "", file);
    },
    [send, setMessages],
  );

  const isRunning = useCallback(() => runningRef.current, []);

  return { running, isRunning, send, retry };
}

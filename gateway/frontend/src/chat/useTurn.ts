// The streaming state machine for one turn.
//
// send() appends the person's message and an empty Joshua message in the
// `pending` state, then streams the answer into it:
//   accepted       -> Joshua has the turn and will finish it, whoever is
//                     connected: the saved draft goes (draft.ts)
//   status         -> `queued` shows "Waiting for Joshua to finish another
//                     turn"; `running` shows "working"
//   delta          -> append the text
//   done           -> set the text to done.text; state ok
//   error frame    -> state failed, with the message; partial text is kept
//   non-2xx answer -> remove the empty Joshua message; the person message
//                     carries the refusal, and onRefused shows a toast
//   lost stream    -> before `accepted`: state lost, with Retry. After
//                     `accepted`: the turn goes on without this screen. The
//                     local messages go, and onDetached(turnId) lets the page
//                     watch the history for the answer (useHistory.ts).
// A turn can carry one photo or one PDF. The file goes with the request as
// multipart; the person message keeps its name for the chip, and the file
// itself, so Retry sends the same message again.
// A phone that locks or switches app can leave the stream open on the screen
// with no connection behind it: the browser may never raise an error. The
// viewer sends a keepalive every 20 s, so a stream with no byte for STALE_MS
// is dead. When the page comes back (visible, pageshow from the back-forward
// cache, focus, online) a stale stream is dropped: before `accepted` the turn
// is lost (Retry, with the draft kept); after it, the turn goes on without this
// screen, as for a stream that broke.
// `upload` is the state of the progress toast of a turn with a file:
// uploading with a percent, then saving (the bytes are out, the server has not
// said it has them). It goes as soon as Joshua has accepted the turn: the end
// of the message list says that the turn runs, so nothing covers the
// conversation while it does. While the bytes are going out, the page asks the
// browser to warn before it is closed, and a hidden page saves the draft again.
// One turn runs at a time. After a `done`, onDone runs, once the turn is
// no longer running; the chat page refreshes the history then. When the
// finished answer carries an `artifact` fence, onArtifact gets the document
// path, so the panel can reload a frame that has that document open
// (DocumentPanel.tsx): the refine loop inside one turn. The path comes from
// the answer and not from the stream, because the `done` frame's `tools` is
// a de-duped list of tool names and never says which document was written.
// A reply that is only `[IGNORE]` (silent.ts) is not shown: its message is
// removed when the turn is done, and while it streams, a text that is only a
// start of the marker is held back, so "[IGN" never shows.
// `workspace` is passed through to the turn body: the turn runs in that
// workspace, and without it in the main thread.
//
// A message written while a turn runs is not refused: send() holds it on the
// server (`held`, the hold of this thread) instead of starting a second turn,
// and the screen shows it as waiting. The hold is the server's, so a reload
// or a closed tab keeps it. dropHeld() takes one back before it goes.
// flushHeld() empties the hold and sends every held message as one turn, in
// order, one per line: the page calls it once no turn of the thread runs any
// more. The server empties the hold in one step, so two screens never send
// the same held message twice.
// A held message can carry one photo or one PDF. The hold write goes out as
// multipart, with the upload toast of a turn with a file, and the server lands
// the file and names it on the waiting message. The take of such a hold answers
// with a ticket, and the turn that sends the taken text carries it as
// `heldTake`: the files follow the ticket, so the page never names a stored
// file.

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { holdReload } from "../pwa";
import {
  addHeld,
  ApiError,
  ConnectionLost,
  dropHeld as dropHeldCall,
  listHeld,
  postTurn,
  takeHeld,
  type Held,
} from "../api/client";
import { clearDraft, saveDraft } from "./draft";
import { parseArtifact } from "./ArtifactCard";
import { ARTIFACT_INFO, splitFence } from "./blocks";
import { isSilentReply, maybeSilentReply } from "./silent";
import { appendMessage, newId, updateMessage, type ChatMessage, type HeldMessage } from "./store";

type SetMessages = Dispatch<SetStateAction<ChatMessage[]>>;

// The document path of the finished answer's `artifact` fence, or null when
// it has none, or one that is not a document path.
function writtenPath(text: string): string | null {
  const { content } = splitFence(text, ARTIFACT_INFO);
  if (!content) return null;
  return parseArtifact(content)?.path ?? null;
}

// No byte for this long on a stream that should send a keepalive every 20 s.
export const STALE_MS = 45_000;

// The progress toast of a turn that carries a file.
export interface Upload {
  phase: "uploading" | "saving";
  // 0 to 100, while uploading.
  percent: number;
  kind: "photo" | "file";
}

export interface Turn {
  running: boolean;
  upload: Upload | null;
  // The running state now, also between a change and the next render.
  isRunning: () => boolean;
  send: (text: string, file?: File | null) => boolean;
  retry: (message: ChatMessage) => void;
  // The messages that wait for the running turn to end, oldest first.
  held: HeldMessage[];
  // Takes one held message back, before it goes.
  dropHeld: (id: string) => void;
  // Sends every held message as one turn. It does nothing while a turn of
  // this screen runs, or while another flush is going out.
  flushHeld: () => void;
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

// What the person is told about the messages the server dropped because they
// waited past its limit. Null when it dropped none.
export function droppedText(dropped: Held[]): string | null {
  if (dropped.length === 0) return null;
  if (dropped.length === 1) return "A waiting message waited too long and was not sent";
  return `${dropped.length} waiting messages waited too long and were not sent`;
}

export function useTurn(
  person: string,
  setMessages: SetMessages,
  onRefused?: (message: string) => void,
  onDone?: () => void,
  onArtifact?: (path: string) => void,
  workspace?: string,
  onDetached?: (turnId: string) => void,
  // A turn of this thread runs that this screen does not stream: the history
  // says it is pending, because another screen or another device sent it.
  otherTurn = false,
): Turn {
  const [running, setRunning] = useState(false);
  const [held, setHeld] = useState<HeldMessage[]>([]);
  const otherTurnRef = useRef(otherTurn);
  otherTurnRef.current = otherTurn;
  const flushingRef = useRef(false);
  const sendRef = useRef<(text: string, file?: File | null, heldTake?: string) => boolean>(
    () => false,
  );
  const [upload, setUpload] = useState<Upload | null>(null);
  const draftRef = useRef<{ text: string; file: File | null } | null>(null);
  const workspaceKey = workspace ?? "";
  const onDetachedRef = useRef(onDetached);
  onDetachedRef.current = onDetached;
  const runningRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  // When a byte last arrived, and the way to drop a stream that has gone quiet.
  const activityRef = useRef(0);
  const dropRef = useRef<(() => void) | null>(null);
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

  // A message the server dropped because it waited too long never goes on its
  // own, so the person is told that it was not sent.
  const tellDropped = useCallback((dropped: Held[]) => {
    const text = droppedText(dropped);
    if (text) onRefusedRef.current?.(text);
  }, []);

  // What the thread held when this screen opened: the messages a page that
  // is gone wrote while a turn ran.
  useEffect(() => {
    let live = true;
    void listHeld(person, workspace)
      .then(({ held: list, dropped }) => {
        if (!live) return;
        setHeld((shown) => [...list, ...shown.filter((m) => m.sending)]);
        tellDropped(dropped);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [person, workspace, tellDropped]);

  // One message that waits for the running turn. It shows at once, and the
  // server's answer replaces it, so the hold on screen is the hold core has.
  const holdMessage = useCallback(
    async (text: string, file?: File | null) => {
      const localId = newId();
      const kind = file?.type.startsWith("image/") ? "photo" : "file";
      setHeld((list) => [
        ...list,
        {
          id: localId,
          text,
          at: new Date().toISOString(),
          sending: true,
          ...(file ? { fileName: file.name } : {}),
        },
      ]);
      if (file) setUpload({ phase: "uploading", percent: 0, kind });
      try {
        const { held: list, dropped } = await addHeld(person, text, workspace, file, {
          onUploadProgress: (loaded, total) => {
            const percent = Math.min(100, Math.floor((loaded / total) * 100));
            setUpload({ phase: percent >= 100 ? "saving" : "uploading", percent, kind });
          },
        });
        setHeld((shown) => [...list, ...shown.filter((m) => m.sending && m.id !== localId)]);
        tellDropped(dropped);
      } catch (err) {
        setHeld((shown) => shown.filter((m) => m.id !== localId));
        onRefusedRef.current?.(errorText(err, "the message could not be held"));
      } finally {
        if (file) setUpload(null);
      }
    },
    [person, workspace, tellDropped, setUpload],
  );

  const dropHeld = useCallback(
    (id: string) => {
      setHeld((list) => list.filter((m) => m.id !== id));
      void dropHeldCall(person, id, workspace)
        .then(({ held: list, dropped }) => {
          setHeld((shown) => [...list, ...shown.filter((m) => m.sending)]);
          tellDropped(dropped);
        })
        .catch((err: unknown) => {
          onRefusedRef.current?.(errorText(err, "the message could not be taken back"));
        });
    },
    [person, workspace, tellDropped],
  );

  const flushHeld = useCallback(() => {
    if (runningRef.current || flushingRef.current) return;
    flushingRef.current = true;
    void takeHeld(person, workspace)
      .then(({ text, takeTicket, dropped }) => {
        setHeld((shown) => shown.filter((m) => m.sending));
        tellDropped(dropped);
        if (text) sendRef.current(text, null, takeTicket);
      })
      .catch((err: unknown) => {
        onRefusedRef.current?.(errorText(err, "the waiting messages could not be sent"));
      })
      .finally(() => {
        flushingRef.current = false;
      });
  }, [person, workspace, tellDropped]);

  // The page is back: a stream with no byte for STALE_MS is dead.
  useEffect(() => {
    const check = () => {
      if (document.visibilityState === "hidden") return;
      if (runningRef.current && Date.now() - activityRef.current > STALE_MS) dropRef.current?.();
    };
    document.addEventListener("visibilitychange", check);
    window.addEventListener("pageshow", check);
    window.addEventListener("focus", check);
    window.addEventListener("online", check);
    return () => {
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("pageshow", check);
      window.removeEventListener("focus", check);
      window.removeEventListener("online", check);
    };
  }, []);

  // While the file is going out, closing the page loses it: ask the browser to
  // warn. A page that is hidden may be thrown away, so the draft is saved again.
  const uploading = upload?.phase === "uploading";
  // A new build of the app does not reload the page while a file is going out.
  const outgoing = upload !== null;
  useEffect(() => (outgoing ? holdReload() : undefined), [outgoing]);
  useEffect(() => {
    if (!uploading) return undefined;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    const hidden = () => {
      const draft = draftRef.current;
      if (document.visibilityState === "hidden" && draft) {
        void saveDraft({ person, workspace: workspaceKey, ...draft, savedAt: Date.now() });
      }
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [uploading, person, workspaceKey]);

  const run = useCallback(
    async (personMsg: ChatMessage, replyId: string, heldTake?: string) => {
      const controller = new AbortController();
      controllerRef.current = controller;
      let completed = false;
      // The document the finished answer says it wrote, if it says one.
      let written: string | null = null;
      // The streamed text so far, which the message shows unless it could
      // still become the silent reply.
      let streamed = "";
      // The id Joshua gave the turn, once it has accepted it.
      let acceptedId: string | null = null;
      // The stream went quiet and was dropped on the page's return.
      let dropped = false;
      activityRef.current = Date.now();
      dropRef.current = () => {
        dropped = true;
        controller.abort();
      };
      const accept = () => {
        draftRef.current = null;
        void clearDraft(person, workspaceKey);
      };
      const patch = (p: Partial<ChatMessage> | ((m: ChatMessage) => Partial<ChatMessage>)) =>
        setMessages((list) => updateMessage(list, replyId, p));
      try {
        await postTurn(
          person,
          personMsg.text,
          (frame) => {
            switch (frame.type) {
              case "accepted":
                acceptedId = frame.turnId || "accepted";
                accept();
                setUpload(null);
                break;
              case "status":
                patch({ waiting: frame.state === "queued" });
                break;
              case "delta":
                setUpload(null);
                streamed += frame.text;
                if (!maybeSilentReply(streamed)) patch({ text: streamed });
                break;
              case "done":
                setUpload(null);
                completed = true;
                if (isSilentReply(frame.text)) {
                  setMessages((list) => list.filter((m) => m.id !== replyId));
                  break;
                }
                patch({ text: frame.text, state: "ok", ...(frame.turnId ? { turnId: frame.turnId } : {}) });
                written = writtenPath(frame.text);
                break;
              case "error":
                setUpload(null);
                patch({ state: "failed", error: frame.message });
                break;
              default:
                break;
            }
          },
          controller.signal,
          personMsg.file,
          workspace,
          {
            onActivity: () => {
              activityRef.current = Date.now();
            },
            onUploadProgress: (loaded, total) => {
              if (acceptedId !== null) return;
              const percent = Math.min(100, Math.floor((loaded / total) * 100));
              const kind = personMsg.file?.type.startsWith("image/") ? "photo" : "file";
              setUpload({ phase: percent >= 100 ? "saving" : "uploading", percent, kind });
            },
          },
          heldTake,
        );
      } catch (err) {
        if (controller.signal.aborted && !dropped) return;
        setUpload(null);
        if (acceptedId !== null && !(err instanceof ApiError)) {
          // Joshua has the turn. The answer will be in the history.
          setMessages((list) => list.filter((m) => m.id !== replyId && m.id !== personMsg.id));
          onDetachedRef.current?.(acceptedId);
        } else if (err instanceof ApiError) {
          accept();
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
        if (dropRef.current && controllerRef.current === null) dropRef.current = null;
        if (!controller.signal.aborted || dropped) {
          runningRef.current = false;
          setRunning(false);
          if (completed) {
            onDoneRef.current?.();
            if (written) onArtifactRef.current?.(written);
          }
        }
      }
    },
    [person, workspace, workspaceKey, setMessages, setUpload],
  );

  const send = useCallback(
    (text: string, file?: File | null, heldTake?: string): boolean => {
      if (!text.trim() && !file) return false;
      // A turn runs: the message waits for it in the hold, on the server, and
      // keeps its place behind the messages that already wait. A file waits
      // with it: the server lands it and gives it to the turn that follows.
      if (runningRef.current || otherTurnRef.current) {
        void holdMessage(text.trim(), file);
        return true;
      }
      runningRef.current = true;
      setRunning(true);
      draftRef.current = { text, file: file ?? null };
      void saveDraft({ person, workspace: workspaceKey, text, file: file ?? null, savedAt: Date.now() });
      if (file) {
        setUpload({ phase: "uploading", percent: 0, kind: file.type.startsWith("image/") ? "photo" : "file" });
      }
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
      void run(personMsg, reply.id, heldTake);
      return true;
    },
    [run, setMessages, setUpload, person, workspaceKey, holdMessage],
  );
  sendRef.current = send;

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

  return { running, upload, isRunning, send, retry, held, dropHeld, flushHeld };
}

// The chat screen, at `/chat/:person`: the header, the message list, and the
// composer, inside the shell of the viewer, so the sidebar and the tabs are
// the viewer's own. The history is Joshua's transcript; this screen holds it
// in memory only. It loads the newest page on open, refreshes it
// (useHistory), and streams turns (useTurn). Open on a document card shows
// the viewer's document page in the panel beside the conversation
// (DocumentPanel); the conversation stays mounted behind it, so closing the
// panel gives back its scroll position. When a turn ends with the `artifact`
// fence of the open document, the panel reloads that frame; while it is open,
// it also watches that document for a change made outside this chat.
//
// `workspace` is for a later phase, which binds a chat to a workspace. It
// goes to the turn and the history calls, and the backend ignores it for now.
// The screen draws nothing for it.

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { Link, useParams } from "react-router-dom";
import Shell from "../shell/Shell";
import { Composer } from "./Composer";
import { DocumentPanel, DocumentPanelProvider, useDocumentPanelState } from "./DocumentPanel";
import { rememberPerson } from "./Home";
import { MessageList } from "./MessageList";
import { validPerson } from "./people";
import { EMPTY_VIEW, type ChatMessage, type View } from "./store";
import { useHistory } from "./useHistory";
import { useTurn } from "./useTurn";

const TOAST_MS = 6000;

function useToast(): [string | null, (message: string) => void] {
  const [toast, setToast] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const show = useCallback((message: string) => {
    clearTimeout(timer.current);
    setToast(message);
    timer.current = setTimeout(() => setToast(null), TOAST_MS);
  }, []);
  return [toast, show];
}

function Chat({ person, workspace }: { person: string; workspace?: string }) {
  const [view, setView] = useState<View>(EMPTY_VIEW);
  const [toast, showToast] = useToast();
  const setMessages = useCallback(
    (update: SetStateAction<ChatMessage[]>) =>
      setView((v) => ({
        ...v,
        messages: typeof update === "function" ? update(v.messages) : update,
      })),
    [],
  );
  // useTurn calls onDone; the history refresh is known only after useHistory.
  const refreshRef = useRef<() => void>(() => undefined);
  const onDone = useCallback(() => refreshRef.current(), []);
  // panel.reload is stable, and reloads the frame only when the document the
  // finished answer wrote is the one the panel has open.
  const panel = useDocumentPanelState();
  const turn = useTurn(person, setMessages, showToast, onDone, panel.reload, workspace);
  const history = useHistory(person, setView, turn.isRunning, showToast, workspace);
  refreshRef.current = history.refresh;

  useEffect(() => {
    rememberPerson(person);
    document.title = `Joshua — ${person}`;
  }, [person]);

  const { loadEarlier } = history;
  const { nextBefore } = view;
  const onLoadEarlier = useCallback(() => {
    if (nextBefore !== null) loadEarlier(nextBefore);
  }, [loadEarlier, nextBefore]);

  return (
    <DocumentPanelProvider panel={panel}>
      <div className="chat-screen">
        <div className="chat">
          <header className="chat-header">
            <div className="title">
              <h1>Joshua</h1>
              <span className="subtitle">{person}</span>
            </div>
          </header>
          <MessageList
            messages={view.messages}
            canRetry={!turn.running}
            onRetry={turn.retry}
            status={history.status}
            error={history.error}
            onReload={history.retry}
            hasEarlier={nextBefore !== null}
            loadingEarlier={history.loadingEarlier}
            onLoadEarlier={onLoadEarlier}
          />
          <Composer onSend={turn.send} busy={turn.running} />
          {toast && (
            <div className="toast" role="alert">
              {toast}
            </div>
          )}
        </div>
        {panel.shown && (
          <DocumentPanel artifact={panel.shown} reloads={panel.reloads} onClose={panel.close} />
        )}
      </div>
    </DocumentPanelProvider>
  );
}

export function ChatPage({ workspace }: { workspace?: string } = {}) {
  const { person = "" } = useParams();
  if (!validPerson(person)) {
    return (
      <Shell section="chat">
        <p>
          This is not a valid person id. <Link to="/chat">Choose another</Link>
        </p>
      </Shell>
    );
  }
  // A new person is a new screen, with its own history.
  return (
    <Shell section="chat">
      <Chat key={`${person}:${workspace ?? ""}`} person={person} workspace={workspace} />
    </Shell>
  );
}

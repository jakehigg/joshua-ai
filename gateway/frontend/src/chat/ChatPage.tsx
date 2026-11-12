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
// The screen has a rail on the left (Rail.tsx): Chat, the workspaces, and
// "+ New workspace". `/chat/:person` is the normal chat, the person's one main
// thread. `/chat/w/:slug` (WorkspacePage) is a workspace of the person who
// signed in: the same chat, with the workspace header and the item panel
// (ItemPanel.tsx). `workspace` goes to the turn and the history calls. Each
// finished turn reloads the items, because the turn may have made some.

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, getJson } from "../api/client";
import { getWorkspace, type Workspace } from "../api/workspaces";
import Shell, { type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";
import { Composer } from "./Composer";
import { NewWorkspace } from "./NewWorkspace";
import { Rail } from "./Rail";
import { ItemPanel } from "./ItemPanel";
import { useWorkspaces, type Workspaces } from "./useWorkspaces";
import { WorkspaceHeader } from "./WorkspaceHeader";
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

interface ChatProps {
  person: string;
  /** The slug sent to the turn and history calls; none is the main thread. */
  workspace?: string;
  /** The workspace on screen, for its header and item panel. */
  ws?: Workspace;
  onWs?: (w: Workspace) => void;
  workspaces: Workspaces;
}

function Chat({ person, workspace, ws, onWs, workspaces }: ChatProps) {
  const navigate = useNavigate();
  const [railOpen, setRailOpen] = useState(false);
  const [itemsOpen, setItemsOpen] = useState(false);
  const [itemsKey, setItemsKey] = useState(0);
  const [creating, setCreating] = useState(false);
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
  const onDone = useCallback(() => {
    refreshRef.current();
    setItemsKey((n) => n + 1);
  }, []);
  // panel.reload is stable, and reloads the frame only when the document the
  // finished answer wrote is the one the panel has open.
  const panel = useDocumentPanelState();
  const turn = useTurn(person, setMessages, showToast, onDone, panel.reload, workspace);
  const history = useHistory(person, setView, turn.isRunning, showToast, workspace);
  refreshRef.current = history.refresh;

  useEffect(() => {
    rememberPerson(person);
    document.title = ws ? `Joshua — ${ws.title}` : `Joshua — ${person}`;
  }, [person, ws]);

  const { loadEarlier } = history;
  const { nextBefore } = view;
  const onLoadEarlier = useCallback(() => {
    if (nextBefore !== null) loadEarlier(nextBefore);
  }, [loadEarlier, nextBefore]);

  return (
    <DocumentPanelProvider panel={panel}>
      <div className="chat-screen">
        <Rail
          person={person}
          workspaces={workspaces.list}
          activeSlug={ws?.slug}
          open={railOpen}
          onNew={() => {
            setRailOpen(false);
            setCreating(true);
          }}
          onNavigate={() => setRailOpen(false)}
        />
        <div className="chat">
          <header className="chat-header">
            <button
              type="button"
              className="rail-toggle"
              aria-label="Show the workspaces"
              aria-expanded={railOpen}
              onClick={() => setRailOpen((o) => !o)}
            >
              ☰
            </button>
            {ws ? (
              <WorkspaceHeader
                workspace={ws}
                onChange={(w) => {
                  onWs?.(w);
                  workspaces.reload();
                }}
                onItems={() => setItemsOpen((o) => !o)}
              />
            ) : (
              <div className="title">
                <h1>Joshua</h1>
                <span className="subtitle">{person}</span>
              </div>
            )}
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
        {ws && (
          <ItemPanel
            slug={ws.slug}
            refreshKey={itemsKey}
            open={itemsOpen}
            onClose={() => setItemsOpen(false)}
          />
        )}
        {panel.shown && (
          <DocumentPanel artifact={panel.shown} reloads={panel.reloads} onClose={panel.close} />
        )}
        {creating && (
          <NewWorkspace
            onClose={() => setCreating(false)}
            onCreated={(created) => {
              setCreating(false);
              workspaces.reload();
              navigate(`/chat/w/${encodeURIComponent(created.slug)}`);
            }}
          />
        )}
      </div>
    </DocumentPanelProvider>
  );
}

export function ChatPage({ workspace }: { workspace?: string } = {}) {
  const { person = "" } = useParams();
  const workspaces = useWorkspaces();
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
      <Chat
        key={`${person}:${workspace ?? ""}`}
        person={person}
        workspace={workspace}
        workspaces={workspaces}
      />
    </Shell>
  );
}

/** `/chat/w/:slug`: a workspace of the person who signed in. */
export function WorkspacePage() {
  const { slug = "" } = useParams();
  const workspaces = useWorkspaces();
  const [person, setPerson] = useState("");
  const [ws, setWs] = useState<Workspace | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    setWs(null);
    setError("");
    Promise.all([getJson<Session>("/session"), getWorkspace(slug)])
      .then(([session, found]) => {
        if (!live) return;
        setPerson(session.person.id);
        setWs(found);
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(
            exc instanceof ApiError && exc.status === 404
              ? "This workspace does not exist."
              : exc instanceof Error
                ? exc.message
                : "the workspace did not load",
          );
        }
      });
    return () => {
      live = false;
    };
  }, [slug]);

  if (error !== "") {
    return (
      <Shell section="chat">
        <ErrorLine text={error} />
        <p>
          <Link to="/chat">Back to the chat</Link>
        </p>
      </Shell>
    );
  }
  if (ws === null) {
    return (
      <Shell section="chat">
        <Spinner label="Loading the workspace" />
      </Shell>
    );
  }
  return (
    <Shell section="chat">
      <Chat
        key={ws.id}
        person={person}
        workspace={ws.slug}
        ws={ws}
        onWs={setWs}
        workspaces={workspaces}
      />
    </Shell>
  );
}

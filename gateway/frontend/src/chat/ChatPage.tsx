// The chat screen, at `/chat/:person`: the header, the message list, and the
// composer, inside the shell of the viewer, so the sidebar and the tabs are
// the viewer's own. The history is Joshua's transcript; this screen holds it
// in memory only. It loads the newest page on open, refreshes it
// (useHistory), and streams turns (useTurn). Open on a document card shows
// the viewer's document page in the panel beside the conversation
// (DocumentPanel); the conversation stays mounted behind it, so closing the
// panel gives back its scroll position. An item of a workspace opens in the
// same panel (itemTarget.ts), as a document, a framed page or a card. When a turn ends with the `artifact`
// fence of the open document, the panel reloads that frame; while it is open,
// it also watches that document for a change made outside this chat.
//
// On a desktop every side column can be folded away: the app sidebar (shell),
// the rail and the item column, each with a « or » button in its header, and
// a ☰ or » button in the chat header that brings it back. The focus button,
// and Ctrl or Cmd + \, fold them all or open them all (panels.ts keeps the
// choice per browser). The phone sheets below do not use this state.
//
// The screen has a rail on the left (Rail.tsx): Chat, the workspaces, and
// "+ New workspace". `/chat/:person` is the normal chat, the person's one main
// thread. `/chat/w/:slug` (WorkspacePage) is a workspace of the person who
// signed in: the same chat, with the workspace header and the item panel
// (ItemPanel.tsx). `workspace` goes to the turn and the history calls. Each
// finished turn reloads the items, because the turn may have made some.
//
// On a phone the rail, the item list and the panel are sheets over the
// conversation, and only one is open at a time: opening one closes the
// others. A rail or item sheet has a dimmed backdrop, and a tap on it closes
// the sheet. Escape closes the top sheet (the panel, then the items, then the
// rail). While a sheet is open the page behind it does not scroll
// (`sheet-open`, index.css). From 900px the rail is a column, and from 1100px
// so is the item list, and the same state drives their close buttons. The
// rail also holds the app navigation links on a phone (Rail.tsx), so the chat
// has one menu button there; a route change closes it.

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, getJson } from "../api/client";
import { getWorkspace, type Workspace } from "../api/workspaces";
import Shell, { type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";
import { Composer } from "./Composer";
import { NewWorkspace } from "./NewWorkspace";
import { usePanels } from "../shell/panels";
import { Rail } from "./Rail";
import { ItemPanel } from "./ItemPanel";
import { useWorkspaces, type Workspaces } from "./useWorkspaces";
import { WorkspaceHeader } from "./WorkspaceHeader";
import {
  DocumentPanel,
  DocumentPanelProvider,
  PagePanel,
  useDocumentPanelState,
} from "./DocumentPanel";
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
  const { pathname } = useLocation();
  const panels = usePanels();
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

  // Only one sheet at a time: a panel that opens closes the lists.
  const { close: closePanel } = panel;
  const panelOpen = panel.shown !== null || panel.view !== null;
  // On a wide desktop the item column steps aside while the panel is open,
  // without changing the folded state that is kept. Items, opened on purpose,
  // closes the panel (toggleItems), and the column is back.
  const itemsCollapsed = panels.collapsed.items || panelOpen;
  useEffect(() => {
    if (panelOpen) {
      setRailOpen(false);
      setItemsOpen(false);
    }
  }, [panelOpen, panel.shown, panel.view]);
  const toggleRail = () => {
    if (!railOpen) {
      setItemsOpen(false);
      closePanel();
    }
    setRailOpen(!railOpen);
  };
  const toggleItems = () => {
    if (!itemsOpen) {
      setRailOpen(false);
      closePanel();
    }
    setItemsOpen(!itemsOpen);
    panels.set("items", false);
  };
  // A route change closes the rail, which holds the app links on a phone.
  useEffect(() => setRailOpen(false), [pathname]);
  // The ☰ gets the focus back when the rail closes.
  const toggleRef = useRef<HTMLButtonElement>(null);
  const railWas = useRef(false);
  useEffect(() => {
    if (railWas.current && !railOpen) toggleRef.current?.focus();
    railWas.current = railOpen;
  }, [railOpen]);
  // Focus mode, on a desktop: Ctrl or Cmd + \ folds every side column away,
  // or opens them all again.
  const { focus } = panels;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "\\") {
        e.preventDefault();
        focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [focus]);
  const sheetOpen = railOpen || itemsOpen || panelOpen;
  useEffect(() => {
    if (!sheetOpen) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (panelOpen) closePanel();
      else if (itemsOpen) setItemsOpen(false);
      else setRailOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sheetOpen, panelOpen, itemsOpen, closePanel]);

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
      <div className={`chat-screen${sheetOpen ? " sheet-open" : ""}${itemsCollapsed ? " items-collapsed" : ""}`}>
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
          onClose={() => setRailOpen(false)}
          collapsed={panels.collapsed.rail}
          onCollapse={() => panels.set("rail", true)}
        />
        {railOpen && (
          <div className="scrim scrim-rail" data-testid="rail-backdrop" onClick={() => setRailOpen(false)} />
        )}
        {itemsOpen && (
          <div className="scrim scrim-items" data-testid="items-backdrop" onClick={() => setItemsOpen(false)} />
        )}
        <div className="chat">
          <header className="chat-header">
            <button
              type="button"
              className="nav-expand"
              aria-label="Open the navigation column"
              aria-expanded={!panels.collapsed.nav}
              aria-controls="app-nav"
              onClick={() => panels.set("nav", false)}
            >
              »
            </button>
            <button
              type="button"
              className="rail-expand"
              aria-label="Open the workspaces column"
              aria-expanded={!panels.collapsed.rail}
              aria-controls="chat-rail"
              onClick={() => panels.set("rail", false)}
            >
              ☰
            </button>
            <button
              type="button"
              ref={toggleRef}
              className="rail-toggle"
              aria-label="Show the workspaces"
              aria-expanded={railOpen}
              onClick={toggleRail}
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
                onItems={toggleItems}
              />
            ) : (
              <div className="title">
                <h1>Joshua</h1>
                <span className="subtitle">{person}</span>
              </div>
            )}
            <button
              type="button"
              className="focus-toggle"
              aria-label="Focus mode"
              aria-pressed={panels.collapsed.nav && panels.collapsed.rail && (!ws || panels.collapsed.items)}
              title="Hide or show every side column (Ctrl or Cmd + \\)"
              onClick={focus}
            >
              ⤢
            </button>
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
            collapsed={itemsCollapsed}
            onCollapse={() => {
              setItemsOpen(false);
              panels.set("items", true);
            }}
          />
        )}
        {panel.shown && (
          <DocumentPanel
            artifact={panel.shown}
            reloads={panel.reloads}
            title={panel.title}
            onClose={panel.close}
          />
        )}
        {panel.view && <PagePanel view={panel.view} onClose={panel.close} />}
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

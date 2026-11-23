// The chat screen, at `/chat/:person`: the header, the message list, and the
// composer, inside the shell of the viewer, so the sidebar and the tabs are
// the viewer's own. The history is Joshua's transcript; this screen holds it
// in memory only. It loads the newest page on open, refreshes it
// (useHistory), and streams turns (useTurn). Open on a document card shows
// the viewer's document page in the panel beside the conversation
// (DocumentPanel); the conversation stays mounted behind it, so closing the
// panel gives back its scroll position. In a workspace the panel is a sheet
// over the dimmed workspace instead (ItemSheet), with a full-screen toggle, so
// the conversation and the item column keep their widths. An item of the
// workspace opens there (itemTarget.ts), as a document, a framed page or a
// card. When a turn ends with the `artifact` fence of the open document, the
// panel reloads that frame; while it is open, it also watches that document
// for a change made outside this chat.
//
// On a desktop every side column can be folded away: the app sidebar (shell)
// and the item column, each with a « or » button in its header, and a » button
// in the chat header that brings it back. The focus button, and Ctrl or
// Cmd + \, fold them all or open them all (panels.ts keeps the choice per
// browser). The phone sheets below do not use this state. The divider beside
// each open panel is a handle: a drag gives that panel another width, and the
// conversation takes the rest. The width stays on the device, for that person,
// that panel and that breakpoint (Divider.tsx, widths.ts).
//
// A message that Joshua has accepted is Joshua's: closing the page or losing
// the signal does not stop the turn. The history says which turn is `pending`
// (View.pending). This screen then ends its message list with a working line
// and loads the history every few seconds until the answer is in. That is how
// a laptop shows the answer to a message sent from a phone, and how this
// screen finds the answer of a stream that broke. A send that never reached
// Joshua leaves its draft in the browser (draft.ts); the next open offers to
// send it again, and a send with a file shows a progress toast.
//
// Text that is typed and not sent is a draft on the server, for this thread
// and this person (useComposerDraft.ts): the composer finds it again after a
// reload, on another device, and when the person comes back from another
// workspace. A send clears it.
//
// A message written while a turn runs is taken and waits: the hold of the
// thread, which lives on the server (useTurn.ts), so a reload or a closed tab
// keeps it. The list ends with each waiting message, and each can be taken
// back. As soon as no turn of the thread runs — the one this screen streams,
// and the `pending` one of the history, which another device may have sent —
// every waiting message goes as one turn, with no action from the person.
//
// While the newest row of a workspace thread is on screen, the screen marks
// that thread read up to it, so the list of the sidebar stops counting it. The
// mark is the signed-in person's own: in a shared workspace every other person
// keeps the count they have.
//
// The one list of the threads is in the app sidebar (shell/ChatList.tsx), so
// this screen draws none. `/chat/:person` is the normal chat, the person's one main
// thread. `/chat/w/:space/:slug` (WorkspacePage) is one workspace: the same
// chat, with the workspace header and the item panel (ItemPanel.tsx). The
// space is `shared` for a shared workspace and the owner for a personal one.
// The screen resolves the address it was opened at once, and after that names
// the workspace by its id alone: the id goes to the turn, the history, the
// read mark and the item calls, because a closed workspace frees its name and
// a ref may move to another row. Every link the screen builds is
// `/chat/w/<id>`, the permanent address of a workspace. A one-segment
// `/chat/w/:ref` opens the workspace of that id, and sends a slug to the id
// (WorkspaceLink). A tap on another workspace in the sidebar only changes that
// ref, and this screen stays mounted, so the workspace it holds carries the
// ref it was loaded for (useWorkspaceOf): until the new ref has its own
// workspace the screen is loading, and the workspace of the ref before never
// sends the browser back to its address. Each finished turn reloads the
// items, because the turn may have made some.
//
// The thread of a shared workspace reads like a group messaging app: the
// messages of the person who is signed in stand at the right, every other
// person's at the left, each writer in their own colour and named
// (MessageList.tsx, Message.tsx). The session gives the name that says which
// rows are theirs.
//
// On a phone the item list and the panel are sheets over the conversation, and
// only one is open at a time: opening one closes the other. Each sheet has a
// dimmed backdrop, and a tap on it closes the sheet. Escape closes the top
// sheet (the panel, then the items). While a sheet is open the page behind it
// does not scroll (`sheet-open`, index.css). From 1100px the item list is a
// column, and the same state drives its close button. The hamburger of the
// chat header opens the navigation drawer of the shell, which holds the
// threads and the app links (Shell.tsx).
//
// `?new=1` on the address of the chat opens the dialog of a new workspace, so
// the "New workspace" button of the sidebar reaches the form that lives here.

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, getJson } from "../api/client";
import { getWorkspace, linkOf, markRead, type Workspace } from "../api/workspaces";
import Shell, { useNavDrawer, type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";
import { Composer } from "./Composer";
import { NewWorkspace } from "./NewWorkspace";
import { usePanels } from "../shell/panels";
import { clearDraft, loadDraft, type Draft } from "./draft";
import { useComposerDraft } from "./useComposerDraft";
import { ItemPanel } from "./ItemPanel";
import { NotesPanel } from "./NotesCard";
import { useWorkspaces, type Workspaces } from "./useWorkspaces";
import { WorkspaceHeader } from "./WorkspaceHeader";
import {
  DocumentPanel,
  DocumentPanelProvider,
  ItemSheet,
  PagePanel,
  useDocumentPanelState,
} from "./DocumentPanel";
import { rememberPerson } from "./Home";
import { Divider } from "./Divider";
import { MessageList } from "./MessageList";
import { validPerson } from "./people";
import { EMPTY_VIEW, type ChatMessage, type View } from "./store";
import { useHistory } from "./useHistory";
import { useTurn, type Upload } from "./useTurn";
import { useKeyboardFit } from "./viewport";
import { PanelWidthsPerson, usePanelWidths } from "./widths";

const TOAST_MS = 6000;
// How long the page waits for a broken stream's turn to show in the history.
const DETACHED_WAIT_MS = 30_000;

function uploadText(upload: Upload): string {
  if (upload.phase === "saving") return `Saving your ${upload.kind}…`;
  return `Uploading ${upload.kind}… ${upload.percent}%`;
}

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
  /**
   * The name of the signed-in person, from the session. In the thread of a
   * shared workspace it says which rows are theirs, so their own messages
   * take the right of the list (MessageList.tsx).
   */
  me?: string;
  /**
   * The id of the workspace, sent to the turn and history calls; none is the
   * main thread. Never a ref: a ref may name another workspace later.
   */
  workspace?: string;
  /** The workspace on screen, for its header and item panel. */
  ws?: Workspace;
  onWs?: (w: Workspace) => void;
  workspaces: Workspaces;
}

function Chat({ person, me, workspace, ws, onWs, workspaces }: ChatProps) {
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const panels = usePanels();
  const widths = usePanelWidths();
  const openDrawer = useNavDrawer();
  const [itemsOpen, setItemsOpen] = useState(false);
  const [itemsKey, setItemsKey] = useState(0);
  const [creating, setCreating] = useState(false);
  const [view, setView] = useState<View>(EMPTY_VIEW);
  // The shell follows the visible part of the page, so the keyboard of a
  // phone never covers the composer or a dialog (viewport.ts).
  useKeyboardFit();
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
  // The turn of a stream that broke after Joshua accepted it, until the
  // history shows it.
  const [detached, setDetached] = useState<string | null>(null);
  // A turn the history names runs too: a message written now waits for it,
  // as it waits for a turn this screen streams.
  const turn = useTurn(
    person,
    setMessages,
    showToast,
    onDone,
    panel.reload,
    workspace,
    setDetached,
    view.pending !== null,
  );
  const polling = !turn.running && (view.pending !== null || detached !== null);
  // A shared workspace has other writers: its history refreshes on its own.
  const history = useHistory(
    person,
    setView,
    turn.isRunning,
    showToast,
    workspace,
    polling,
    ws?.shared ?? false,
  );
  refreshRef.current = history.refresh;
  // The stream is gone: look for the turn now, not at the first poll.
  const { refresh: refreshHistory } = history;
  useEffect(() => {
    if (detached === null) return undefined;
    const timer = setTimeout(refreshHistory, 0);
    return () => clearTimeout(timer);
  }, [detached, refreshHistory]);
  useEffect(() => {
    if (detached === null) return undefined;
    if (view.pending !== null || view.messages.some((m) => m.role === "joshua" && m.turnId === detached)) {
      setDetached(null);
      return undefined;
    }
    const timer = setTimeout(() => setDetached(null), DETACHED_WAIT_MS);
    return () => clearTimeout(timer);
  }, [detached, view]);

  // The messages written while a turn ran go as one turn as soon as no turn
  // of the thread runs: the one this screen streams, and the one the history
  // names, which another device may have sent. The hold lives on the server,
  // so this also sends what a page that is gone left waiting.
  const { held, flushHeld } = turn;
  useEffect(() => {
    if (turn.running || view.pending !== null || history.status !== "ready") return;
    if (held.length > 0) flushHeld();
  }, [turn.running, view.pending, history.status, held, flushHeld]);

  // A message sent here goes into view, in the normal chat and in a
  // workspace alike: the counter rises on each send, and the list jumps to
  // the bottom once the new message has rendered (MessageList.tsx). A message
  // that arrives moves the list only while it already stands at the bottom.
  const [sent, setSent] = useState(0);
  const { send: sendTurn } = turn;
  const send = useCallback(
    (text: string, file?: File | null) => {
      const taken = sendTurn(text, file);
      if (taken) setSent((n) => n + 1);
      return taken;
    },
    [sendTurn],
  );

  // The unsent text of the composer, kept on the server for this thread and
  // this person, so it is there after a reload and on another device.
  const composerDraft = useComposerDraft(person, workspace);

  // A send that did not arrive, kept by the last visit. It is offered again
  // until it is sent, or thrown away, or a new message is sent.
  const [draft, setDraft] = useState<Draft | null>(null);
  const workspaceKey = workspace ?? "";
  useEffect(() => {
    let live = true;
    void loadDraft(person, workspaceKey).then((found) => {
      if (live) setDraft(found);
    });
    return () => {
      live = false;
    };
  }, [person, workspaceKey]);
  useEffect(() => {
    if (turn.running) setDraft(null);
  }, [turn.running]);

  // Only one sheet at a time: a panel that opens closes the lists.
  const { close: closePanel } = panel;
  const panelOpen = panel.shown !== null || panel.view !== null;
  // In a workspace the open item stands over the row as a sheet, so the item
  // column keeps its place behind it and the conversation keeps its width.
  // The column folds only when the person folds it. Items, opened on purpose,
  // closes the sheet (toggleItems).
  const itemsCollapsed = panels.collapsed.items;
  useEffect(() => {
    if (panelOpen) setItemsOpen(false);
  }, [panelOpen, panel.shown, panel.view]);
  const toggleItems = () => {
    if (!itemsOpen) closePanel();
    setItemsOpen(!itemsOpen);
    panels.set("items", false);
  };
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
  const sheetOpen = itemsOpen || panelOpen;
  useEffect(() => {
    if (!sheetOpen) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (panelOpen) closePanel();
      else setItemsOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sheetOpen, panelOpen, closePanel]);

  // `?new=1`, from the "New workspace" button of the sidebar: the dialog opens
  // and the query goes, so a reload of the address does not open it again.
  useEffect(() => {
    if (new URLSearchParams(search).get("new") !== "1") return;
    setCreating(true);
    navigate(pathname, { replace: true });
  }, [search, pathname, navigate]);

  useEffect(() => {
    rememberPerson(person);
    document.title = ws ? `Joshua — ${ws.title}` : `Joshua — ${person}`;
  }, [person, ws]);

  const { loadEarlier } = history;
  const { nextBefore } = view;
  const onLoadEarlier = useCallback(() => {
    if (nextBefore !== null) loadEarlier(nextBefore);
  }, [loadEarlier, nextBefore]);

  // The thread is read as far as the newest row on screen. The mark is this
  // person's own, so in a shared workspace it clears nobody else's count. The
  // list of the sidebar is loaded again, so the count beside the name goes. A
  // mark that fails is not kept, so the next row tries again.
  const { reload: reloadWorkspaces } = workspaces;
  const marked = useRef(0);
  const onRead = useCallback(
    (serverId: number) => {
      if (!workspace || serverId <= marked.current) return;
      marked.current = serverId;
      markRead(workspace, serverId)
        .then(reloadWorkspaces)
        .catch(() => {
          marked.current = 0;
        });
    },
    [workspace, reloadWorkspaces],
  );

  return (
    <DocumentPanelProvider panel={panel}>
      <div
        className={`chat-screen${sheetOpen ? " sheet-open" : ""}${itemsCollapsed ? " items-collapsed" : ""}`}
        style={widths.style}
      >
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
              className="j-nav-toggle"
              aria-label="Show the navigation"
              aria-controls="app-nav"
              onClick={(e) => openDrawer(e.currentTarget)}
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
              aria-pressed={panels.collapsed.nav && (!ws || panels.collapsed.items)}
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
            pending={turn.running ? null : view.pending}
            held={turn.held}
            onDropHeld={turn.dropHeld}
            me={me}
            shared={ws?.shared ?? false}
            onRead={onRead}
            sent={sent}
          />
          {draft && (
            <div className="draft-banner" role="alert">
              <span>
                {draft.file
                  ? `Your ${draft.file.type.startsWith("image/") ? "photo" : "file"} didn't finish uploading (${draft.file.name}).`
                  : "Your last message didn't finish sending."}
              </span>
              <button
                type="button"
                className="retry"
                onClick={() => {
                  setDraft(null);
                  send(draft.text, draft.file);
                }}
              >
                Retry
              </button>
              <button
                type="button"
                className="retry"
                onClick={() => {
                  setDraft(null);
                  void clearDraft(person, workspaceKey);
                }}
              >
                Discard
              </button>
            </div>
          )}
          <Composer onSend={send} draft={composerDraft.text} onDraft={composerDraft.save} />
          {turn.upload && (
            <div className="toast toast-progress" role="status">
              {uploadText(turn.upload)}
            </div>
          )}
          {toast && (
            <div className="toast" role="alert">
              {toast}
            </div>
          )}
        </div>
        {ws && !itemsCollapsed && <Divider panel="items" label="Resize the items column" />}
        {ws && (
          <ItemPanel
            workspace={ws.id}
            refreshKey={itemsKey}
            open={itemsOpen}
            onClose={() => setItemsOpen(false)}
            collapsed={itemsCollapsed}
            onCollapse={() => {
              setItemsOpen(false);
              panels.set("items", true);
            }}
            notes={ws}
          />
        )}
        {ws && panelOpen && (
          <ItemSheet
            panel={panel}
            renderNotes={(fullToggle) => (
              <NotesPanel
                key={ws.id}
                notes={{
                  workspace: ws,
                  onSaved: (w) => {
                    onWs?.(w);
                    workspaces.reload();
                  },
                  onAsk: (text) => send(text),
                }}
                onClose={panel.close}
                fullToggle={fullToggle}
              />
            )}
          />
        )}
        {!ws && panelOpen && <Divider panel="doc" label="Resize the document panel" />}
        {!ws && panel.shown && (
          <DocumentPanel
            artifact={panel.shown}
            reloads={panel.reloads}
            title={panel.title}
            onClose={panel.close}
          />
        )}
        {!ws && panel.view && <PagePanel view={panel.view} onClose={panel.close} />}
        {creating && (
          <NewWorkspace
            onClose={() => setCreating(false)}
            onCreated={(created) => {
              setCreating(false);
              workspaces.reload();
              navigate(linkOf(created));
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
      <PanelWidthsPerson value={person}>
        <Chat
          key={`${person}:${workspace ?? ""}`}
          person={person}
          workspace={workspace}
          workspaces={workspaces}
        />
      </PanelWidthsPerson>
    </Shell>
  );
}

/**
 * What one ref loaded: the workspace of that ref, or the reason it did not
 * load. The ref is in the same state value as its answer, because a switch
 * from one workspace to another keeps this screen mounted and only changes
 * the ref: React renders with the new ref before the effect that loads it
 * runs. The answer of the ref before is then not the answer of the ref now,
 * and the screen is still loading.
 */
interface Loaded {
  reference: string;
  ws: Workspace | null;
  error: string;
}

const NOTHING_LOADED: Loaded = { reference: "", ws: null, error: "" };

// The workspace a ref names, and who is signed in: their id and their name,
// the name the thread of a shared workspace shows on a row. `null` while it
// loads, and while the ref of the answer is not the ref asked for.
function useWorkspaceOf(reference: string): {
  person: string;
  name: string;
  ws: Workspace | null;
  setWs: (w: Workspace) => void;
  error: string;
} {
  const [person, setPerson] = useState("");
  const [name, setName] = useState("");
  const [loaded, setLoaded] = useState<Loaded>(NOTHING_LOADED);

  useEffect(() => {
    let live = true;
    Promise.all([getJson<Session>("/session"), getWorkspace(reference)])
      .then(([session, found]) => {
        if (!live) return;
        setPerson(session.person.id);
        setName(session.person.name);
        setLoaded({ reference, ws: found, error: "" });
      })
      .catch((exc: unknown) => {
        if (live) {
          setLoaded({
            reference,
            ws: null,
            error:
              exc instanceof ApiError && exc.status === 404
                ? "This workspace does not exist."
                : exc instanceof Error
                  ? exc.message
                  : "the workspace did not load",
          });
        }
      });
    return () => {
      live = false;
    };
  }, [reference]);

  const setWs = useCallback(
    (w: Workspace) => setLoaded({ reference, ws: w, error: "" }),
    [reference],
  );
  const answer = loaded.reference === reference ? loaded : NOTHING_LOADED;

  return { person, name, ws: answer.ws, setWs, error: answer.error };
}

function WorkspaceError({ text }: { text: string }) {
  return (
    <Shell section="chat">
      <ErrorLine text={text} />
      <p>
        <Link to="/chat">Back to the chat</Link>
      </p>
    </Shell>
  );
}

/**
 * The screen of the workspace a ref names: the chat, with the workspace
 * header and the item panel. With `toId`, a ref that is not the id of the
 * workspace sends the browser to `/chat/w/<id>` instead, the permanent
 * address.
 */
function WorkspaceScreen({ reference, toId = false }: { reference: string; toId?: boolean }) {
  const workspaces = useWorkspaces();
  const { person, name, ws, setWs, error } = useWorkspaceOf(reference);

  if (error !== "") {
    return <WorkspaceError text={error} />;
  }
  if (ws === null) {
    return (
      <Shell section="chat">
        <Spinner label="Loading the workspace" />
      </Shell>
    );
  }
  if (toId && reference !== ws.id) {
    return <Navigate to={linkOf(ws)} replace />;
  }
  return (
    <Shell section="chat">
      <PanelWidthsPerson value={person}>
        <Chat
          key={ws.id}
          person={person}
          me={name}
          workspace={ws.id}
          ws={ws}
          onWs={setWs}
          workspaces={workspaces}
        />
      </PanelWidthsPerson>
    </Shell>
  );
}

/**
 * `/chat/w/:space/:slug`: one workspace. `:space` is `shared` for a shared
 * workspace and the id of the owner for the workspace of a person, so every
 * person in a shared workspace opens it at the same address.
 */
export function WorkspacePage() {
  const { space = "", slug = "" } = useParams();
  return <WorkspaceScreen reference={`${space}/${slug}`} />;
}

/**
 * `/chat/w/:ref`: one segment, the id of a workspace or a slug. The id is the
 * permanent address and opens the workspace here; any other ref finds the
 * workspace and sends the browser there, so an old link, and a link a person
 * kept, still opens the workspace it named.
 */
export function WorkspaceLink() {
  const { ref = "" } = useParams();
  return <WorkspaceScreen reference={ref} toId />;
}

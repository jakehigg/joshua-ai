// The viewer's own document page, inside the chat.
//
// Open on a document card (ArtifactCard.tsx) shows this panel: a same-origin
// <iframe> of the page the viewer serves at /documents/<owner>/<slug>. There
// is no second renderer, no srcdoc and no embed mode, so the frame has the
// viewer's blocks, its `+` gutter for a comment per block, its Versions fold
// and `?rev=<sha>` for an older version. A comment made in the frame posts
// with the viewer's own CSRF token, to the viewer's own origin, so it lands
// on the document with no work here. The frame carries no `sandbox`: a
// sandbox would drop the cookie and the token the comment form needs.
//
// The src is a host-absolute path, and the frame loads it as a page of its
// own: it is never a router <Link>, because the frame has its own app and
// the chat screen stays where it is.
//
// The layout: a column beside the conversation on a desktop, a sheet over it
// on a phone (index.css). The conversation stays mounted in both, so closing
// the panel gives back the scroll position it had.
//
// reload() is the refine loop inside one turn: the person comments on lines
// in the panel, says "apply them", and the finished answer carries the
// `artifact` fence of that same document (useTurn.ts). The frame then shows
// the new body, with a fresh cache-busting query. A fence naming another
// document reloads nothing.
//
// A change made outside this chat — by an assignment, or from another
// device — has no turn to tell of it, so the open panel also polls the
// viewer's JSON route for the document (useDocumentWatch below).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getDocumentMeta } from "../api/client";
import type { Artifact } from "./ArtifactCard";

// How often an open panel asks the viewer whether its document changed.
export const POLL_MS = 15_000;

// The one line of affordance in the panel: what the `+` gutter is for.
export const COMMENT_HINT = "Comment on a line with +; Joshua reads the comments on the next turn.";

export interface DocumentPanelState {
  /** The document the panel shows, or null when no panel is open. */
  shown: Artifact | null;
  /**
   * How often the frame was loaded again since the panel opened: by a turn
   * that wrote the document, or by the watch that saw it change.
   */
  reloads: number;
  show: (artifact: Artifact) => void;
  close: () => void;
  /** Reload the frame, when `path` is the document the panel has open. */
  reload: (path: string) => void;
}

const CLOSED: DocumentPanelState = {
  shown: null,
  reloads: 0,
  show: () => undefined,
  close: () => undefined,
  reload: () => undefined,
};

const Context = createContext<DocumentPanelState>(CLOSED);

// While the panel is open and the tab is visible, ask the viewer every
// POLL_MS whether the open document's `updated` stamp changed, and reload
// the frame once when it did. This catches the change a turn of this chat
// does not make.
//
// The timer runs for an open panel in a visible tab only: it stops when the
// panel closes and when the tab goes away, and starts again when the tab
// comes back, the way useHistory.ts refreshes on visibility. A start asks at
// once, so a change made while the tab was away shows as it comes back.
//
// `reloads` is a dependency: each reload of the frame, by a turn or by this
// timer, starts the watch again, and its first answer is the new baseline.
// So a document the turn just wrote is not loaded a second time. A failed
// call changes no baseline and reloads nothing.
function useDocumentWatch(url: string | undefined, reloads: number, reload: () => void): void {
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    if (!url) return undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    let pending: AbortController | null = null;
    let seen: string | null = null;
    let gone = false;

    const ask = async () => {
      const controller = new AbortController();
      pending = controller;
      const meta = await getDocumentMeta(url, controller.signal);
      if (pending === controller) pending = null;
      if (gone || controller.signal.aborted || !meta?.updated) return;
      if (seen !== null && meta.updated !== seen) reloadRef.current();
      seen = meta.updated;
    };
    const start = () => {
      if (timer !== undefined) return;
      timer = setInterval(() => void ask(), POLL_MS);
      void ask();
    };
    const stop = () => {
      clearInterval(timer);
      timer = undefined;
      pending?.abort();
      pending = null;
    };
    const onVisibility = () => (document.visibilityState === "visible" ? start() : stop());

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      gone = true;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [url, reloads]);
}

/** The panel state of a chat screen; the screen puts it in the context. */
export function useDocumentPanelState(): DocumentPanelState {
  const [shown, setShown] = useState<Artifact | null>(null);
  const [reloads, setReloads] = useState(0);
  // reload() is called from a turn's `done`, and must not change with each
  // render of the screen, so it reads the open document from a ref.
  const shownRef = useRef<Artifact | null>(null);
  shownRef.current = shown;
  const show = useCallback((artifact: Artifact) => {
    setShown(artifact);
    setReloads(0);
  }, []);
  const close = useCallback(() => setShown(null), []);
  const bump = useCallback(() => setReloads((n) => n + 1), []);
  const reload = useCallback(
    (path: string) => {
      if (shownRef.current?.path === path) bump();
    },
    [bump],
  );
  useDocumentWatch(shown?.url, reloads, bump);
  return useMemo(() => ({ shown, reloads, show, close, reload }), [shown, reloads, show, close, reload]);
}

/** The panel a card opens. Outside a chat screen, a card opens nothing. */
export function useDocumentPanel(): DocumentPanelState {
  return useContext(Context);
}

export function DocumentPanelProvider({
  panel,
  children,
}: {
  panel: DocumentPanelState;
  children: ReactNode;
}) {
  return <Context.Provider value={panel}>{children}</Context.Provider>;
}

// The src of the frame: the document's page, with a cache-busting query
// once the turn wrote the document again.
function frameSrc(artifact: Artifact, reloads: number): string {
  return reloads > 0 ? `${artifact.url}?r=${reloads}` : artifact.url;
}

export function DocumentPanel({
  artifact,
  reloads = 0,
  onClose,
}: {
  artifact: Artifact;
  reloads?: number;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const src = frameSrc(artifact, reloads);
  // On a phone the panel covers the conversation, so the keyboard and a
  // reader must land in it, on the control that closes it again.
  useEffect(() => {
    closeRef.current?.focus();
  }, [artifact.url]);
  return (
    <aside className="doc-panel" aria-label={`${artifact.slug}, the document`}>
      <header className="doc-panel-header">
        <span className="doc-panel-path">{artifact.path}</span>
        <button
          type="button"
          ref={closeRef}
          className="doc-panel-close"
          aria-label="Close the document"
          onClick={onClose}
        >
          ✕
        </button>
      </header>
      <p className="doc-panel-hint">{COMMENT_HINT}</p>
      {/* A new document, and a reload of this one, is a new frame, not a
          navigation of this one, which would put the document in the
          browser's own history. */}
      <iframe
        key={src}
        className="doc-panel-frame"
        src={src}
        title={`${artifact.slug}, the document page`}
      />
    </aside>
  );
}

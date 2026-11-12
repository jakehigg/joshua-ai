// The left rail of the chat screen: Chat, the active workspaces, the paused
// ones (folded), and "+ New workspace". Under them it repeats the app
// navigation links of the shell (AppNav), without Chat, which the first entry
// of the rail already is, which the shell's own sidebar shows
// only from 760px; so on a phone this drawer is the one menu of the chat.
// On a desktop it folds away with « (the chat header brings it back).
// On a phone it is a drawer with a close
// button; the screen also closes it on a tap of the backdrop and on Escape.

import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import type { Workspace } from "../api/workspaces";
import AppNav, { useNav } from "../shell/AppNav";

export function Rail({
  person,
  workspaces,
  activeSlug,
  open,
  onNew,
  onNavigate,
  onClose,
  collapsed = false,
  onCollapse,
}: {
  person: string;
  workspaces: Workspace[];
  /** The slug of the workspace on screen, or undefined in the normal chat. */
  activeSlug?: string;
  open: boolean;
  onNew: () => void;
  onNavigate: () => void;
  onClose: () => void;
  /** A desktop column that is folded away (index.css). */
  collapsed?: boolean;
  onCollapse?: () => void;
}) {
  const nav = useNav();
  const closeRef = useRef<HTMLButtonElement>(null);
  // The drawer takes the focus when it opens, on its close button, so no link
  // looks selected.
  useEffect(() => {
    if (open) closeRef.current?.focus();
  }, [open]);
  const active = workspaces.filter((w) => w.status === "active");
  const paused = workspaces.filter((w) => w.status === "paused");
  const row = (w: Workspace) => (
    <li key={w.id}>
      <Link
        to={`/chat/w/${encodeURIComponent(w.slug)}`}
        aria-current={w.slug === activeSlug ? "page" : undefined}
        onClick={onNavigate}
      >
        {w.title}
      </Link>
    </li>
  );
  return (
    <nav
      id="chat-rail"
      className={`rail${open ? " open" : ""}${collapsed ? " collapsed" : ""}`}
      aria-label="Chats and workspaces"
    >
      <header className="rail-header">
        <span className="rail-title">
          <img className="j-logo" src="/logo-64.png" alt="" />
          Joshua
        </span>
        <button type="button" ref={closeRef} className="rail-close" onClick={onClose} aria-label="Close the workspaces">
          ✕
        </button>
        <button
          type="button"
          className="rail-collapse"
          aria-label="Hide the workspaces"
          aria-expanded={!collapsed}
          aria-controls="chat-rail"
          onClick={onCollapse}
        >
          «
        </button>
      </header>
      <ul>
        <li>
          <Link
            to={`/chat/${encodeURIComponent(person)}`}
            aria-current={activeSlug === undefined ? "page" : undefined}
            onClick={onNavigate}
          >
            Chat
          </Link>
        </li>
      </ul>
      <h2 className="rail-label">Workspaces</h2>
      <ul>{active.map(row)}</ul>
      {paused.length > 0 && (
        <details>
          <summary>Paused ({paused.length})</summary>
          <ul>{paused.map(row)}</ul>
        </details>
      )}
      <button type="button" className="rail-new" onClick={onNew}>
        + New workspace
      </button>
      {nav && (
        <div className="rail-appnav">
          <h2 className="rail-label">Navigate</h2>
          <AppNav
            sections={nav.sections}
            section={nav.section}
            onNavigate={onNavigate}
            omit={["chat"]}
          />
        </div>
      )}
    </nav>
  );
}

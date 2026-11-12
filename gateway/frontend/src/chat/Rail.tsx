// The left rail of the chat screen: Chat, the active workspaces, the paused
// ones (folded), and "+ New workspace". On a phone it is a drawer with a close
// button; the screen also closes it on a tap of the backdrop and on Escape.

import { Link } from "react-router-dom";
import type { Workspace } from "../api/workspaces";

export function Rail({
  person,
  workspaces,
  activeSlug,
  open,
  onNew,
  onNavigate,
  onClose,
}: {
  person: string;
  workspaces: Workspace[];
  /** The slug of the workspace on screen, or undefined in the normal chat. */
  activeSlug?: string;
  open: boolean;
  onNew: () => void;
  onNavigate: () => void;
  onClose: () => void;
}) {
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
    <nav className={open ? "rail open" : "rail"} aria-label="Chats and workspaces">
      <header className="rail-header">
        <span className="rail-title">Joshua</span>
        <button type="button" className="rail-close" onClick={onClose} aria-label="Close the workspaces">
          ✕
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
    </nav>
  );
}

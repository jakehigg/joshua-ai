// The item panel of a workspace, at the right of the chat: what was built or
// collected for it, grouped by kind. Every item opens inside the chat screen,
// in the panel (itemTarget.ts): a document or artifact, a page of the viewer,
// or a preview card for an external link. Nothing navigates the chat tab. An
// item with an unsafe address (links.ts) is shown as text. The item that is
// open in the panel is marked in the list.
//
// A row is one line: a kind icon, the title (cut with an ellipsis, the whole
// title on hover), a pin mark, and a "..." button. The button opens a small
// menu with Pin or Unpin and Remove. Remove hides the row at once and says so
// for a few seconds with an Undo; the call goes out when the time is up. A
// pinned item is first in its group. A group folds with its header, and the
// folded groups are kept per browser in localStorage (try/catch; without it
// every group is open). The form that adds a link is behind "+ Add link".
//
// On a phone the screen closes this list when an item opens, so the panel is
// visible. The list is a sheet there, with a close button. On a wide desktop
// it is a column that folds away with » (the workspace header brings it back),
// and it also steps aside while the panel is open (ChatPage.tsx). The list
// loads on open, after each turn (`refreshKey`) and when the tab gets focus
// back.

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  addItem,
  listItems,
  pinItem,
  removeItem,
  type WorkspaceItem,
} from "../api/workspaces";
import { useDocumentPanel } from "./DocumentPanel";
import { itemTarget } from "./itemTarget";

const KIND_LABELS: Record<string, string> = {
  document: "Documents",
  artifact: "Artifacts",
  plan: "Plans",
  wiki: "Wiki pages",
  tasks_project: "Task projects",
  task: "Tasks",
  assignment: "Assignments",
  developer_task: "Developer tasks",
  pr: "Pull requests",
  link: "Links",
  file: "Files",
};

export function groupItems(items: WorkspaceItem[]): [string, WorkspaceItem[]][] {
  const groups = new Map<string, WorkspaceItem[]>();
  for (const item of items) groups.set(item.kind, [...(groups.get(item.kind) ?? []), item]);
  const known = Object.keys(KIND_LABELS).filter((k) => groups.has(k));
  const other = [...groups.keys()].filter((k) => !(k in KIND_LABELS));
  return [...known, ...other].map((k) => [k, groups.get(k) ?? []]);
}

function label(item: WorkspaceItem): string {
  return item.title.trim() || item.ref;
}

const KIND_ICONS: Record<string, string> = {
  document: "📄",
  artifact: "🎨",
  plan: "📋",
  wiki: "📚",
  tasks_project: "🗂",
  task: "☑",
  assignment: "📝",
  developer_task: "🛠",
  pr: "🔀",
  link: "🔗",
  file: "📎",
};

/** How long a removed row can be brought back. */
export const UNDO_MS = 5000;

export const GROUPS_KEY = "joshua.items.collapsedGroups";

function readGroups(): string[] {
  try {
    const raw = JSON.parse(window.localStorage.getItem(GROUPS_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((k): k is string => typeof k === "string") : [];
  } catch {
    return [];
  }
}

function saveGroups(groups: string[]): void {
  try {
    window.localStorage.setItem(GROUPS_KEY, JSON.stringify(groups));
  } catch {
    // Kept in memory only.
  }
}

function Entry({
  item,
  active,
  menuOpen,
  onMenu,
  onPin,
  onRemove,
}: {
  item: WorkspaceItem;
  active: boolean;
  menuOpen: boolean;
  onMenu: () => void;
  onPin: () => void;
  onRemove: () => void;
}) {
  const panel = useDocumentPanel();
  const target = itemTarget(item);
  const text = label(item);
  const name = target ? (
    <button
      type="button"
      className="item-open"
      title={text}
      aria-current={active ? "true" : undefined}
      onClick={() => panel.open(target)}
    >
      {text}
    </button>
  ) : (
    <span className="item-text" title={text}>
      {text}
    </span>
  );
  return (
    <li className={active ? "item active" : "item"}>
      <span className="item-icon" aria-hidden="true">
        {KIND_ICONS[item.kind] ?? "•"}
      </span>
      {name}
      {item.pinned && (
        <span className="item-pin" role="img" aria-label="Pinned">
          📌
        </span>
      )}
      <span className="item-menu-wrap">
        <button
          type="button"
          className="item-more"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-label={`Actions for ${text}`}
          onClick={onMenu}
        >
          ⋯
        </button>
        {menuOpen && (
          <div className="item-menu" role="menu">
            <button type="button" role="menuitem" aria-label={`${item.pinned ? "Unpin" : "Pin"} ${text}`} onClick={onPin}>
              {item.pinned ? "Unpin" : "Pin"}
            </button>
            <button type="button" role="menuitem" aria-label={`Remove ${text}`} onClick={onRemove}>
              Remove
            </button>
          </div>
        )}
      </span>
    </li>
  );
}

export function ItemPanel({
  slug,
  refreshKey,
  open,
  onClose,
  collapsed = false,
  onCollapse,
}: {
  slug: string;
  refreshKey: number;
  open: boolean;
  onClose: () => void;
  /** A desktop column that is folded away (index.css). */
  collapsed?: boolean;
  onCollapse?: () => void;
}) {
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const panel = useDocumentPanel();
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [folded, setFolded] = useState<string[]>(readGroups);
  // A row that was removed and can still come back, and its timer.
  const [undo, setUndo] = useState<{ item: WorkspaceItem; timer: ReturnType<typeof setTimeout> } | null>(null);
  const undoRef = useRef(undo);
  undoRef.current = undo;

  const load = useCallback(() => {
    listItems(slug)
      .then((rows) => {
        setItems(rows);
        setError("");
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "the items did not load"));
  }, [slug]);

  useEffect(load, [load, refreshKey]);
  useEffect(() => {
    const again = () => {
      if (document.visibilityState === "visible") load();
    };
    window.addEventListener("focus", again);
    document.addEventListener("visibilitychange", again);
    return () => {
      window.removeEventListener("focus", again);
      document.removeEventListener("visibilitychange", again);
    };
  }, [load]);

  const act = (call: Promise<unknown>) =>
    call.then(load).catch((e: unknown) => setError(e instanceof Error ? e.message : "failed"));

  // The menu of a row closes on a tap outside it and on Escape.
  useEffect(() => {
    if (menuFor === null) return undefined;
    const outside = (e: Event) => {
      if (!(e.target as HTMLElement).closest(".item-menu-wrap")) setMenuFor(null);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuFor(null);
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", key);
    };
  }, [menuFor]);

  // A removal that is waiting goes out when the panel goes away, so the
  // choice is not lost.
  const flush = useCallback(() => {
    const waiting = undoRef.current;
    if (!waiting) return;
    clearTimeout(waiting.timer);
    setUndo(null);
    void act(removeItem(slug, waiting.item.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, load]);
  useEffect(
    () => () => {
      const waiting = undoRef.current;
      if (waiting) {
        clearTimeout(waiting.timer);
        void removeItem(slug, waiting.item.id).catch(() => undefined);
      }
    },
    [slug],
  );
  const remove = (item: WorkspaceItem) => {
    setMenuFor(null);
    flush();
    const timer = setTimeout(() => {
      setUndo(null);
      void act(removeItem(slug, item.id));
    }, UNDO_MS);
    setUndo({ item, timer });
  };
  const undoRemove = () => {
    if (undo) clearTimeout(undo.timer);
    setUndo(null);
  };

  const toggleGroup = (kind: string) => {
    const next = folded.includes(kind) ? folded.filter((k) => k !== kind) : [...folded, kind];
    setFolded(next);
    saveGroups(next);
  };

  // The item the panel shows, if it is one of these.
  const isOpen = (item: WorkspaceItem): boolean => {
    const t = itemTarget(item);
    if (!t) return false;
    if (t.type === "document") return panel.shown?.url === t.artifact.url;
    if (t.type === "page") return panel.view?.type === "page" && panel.view.page.url === t.page.url;
    return (
      panel.view?.type === "card" &&
      panel.view.card.title === t.card.title &&
      panel.view.card.address === t.card.address
    );
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const address = url.trim();
    if (!address) return;
    void act(addItem(slug, { kind: "link", ref: address, title: name.trim(), url: address }));
    setUrl("");
    setName("");
    setAdding(false);
  };

  const shown = items.filter((item) => item.id !== undo?.item.id);
  const ordered = [...shown].sort((a, b) => Number(b.pinned) - Number(a.pinned));
  return (
    <aside
      id="workspace-items"
      className={`items${open ? " open" : ""}${collapsed ? " collapsed" : ""}`}
      aria-label="Workspace items"
    >
      <header className="items-header">
        <h2>Items</h2>
        <button type="button" className="items-close" onClick={onClose} aria-label="Close the items">
          ✕
        </button>
        <button
          type="button"
          className="items-collapse"
          aria-label="Hide the items"
          aria-expanded={!collapsed}
          aria-controls="workspace-items"
          onClick={onCollapse}
        >
          »
        </button>
      </header>
      {error && (
        <p className="j-error" role="alert">
          {error}
        </p>
      )}
      {undo && (
        <p className="item-undo" role="status">
          <span>Removed “{label(undo.item)}”.</span>
          <button type="button" onClick={undoRemove}>
            Undo
          </button>
        </p>
      )}
      {shown.length === 0 && !error && <p className="j-meta">Nothing here yet.</p>}
      {groupItems(ordered).map(([kind, list]) => {
        const closed = folded.includes(kind);
        return (
          <section key={kind}>
            <h3 className="rail-label item-group">
              <button
                type="button"
                className="item-group-toggle"
                aria-expanded={!closed}
                onClick={() => toggleGroup(kind)}
              >
                <span aria-hidden="true">{closed ? "▸" : "▾"}</span>
                {KIND_LABELS[kind] ?? kind}
                <span className="item-count">{list.length}</span>
              </button>
            </h3>
            {!closed && (
              <ul>
                {list.map((item) => (
                  <Entry
                    key={item.id}
                    item={item}
                    active={isOpen(item)}
                    menuOpen={menuFor === item.id}
                    onMenu={() => setMenuFor(menuFor === item.id ? null : item.id)}
                    onPin={() => {
                      setMenuFor(null);
                      void act(pinItem(slug, item.id, !item.pinned));
                    }}
                    onRemove={() => remove(item)}
                  />
                ))}
              </ul>
            )}
          </section>
        );
      })}
      {adding ? (
        <form className="item-add" onSubmit={submit}>
          <label className="j-field-label" htmlFor="item-url">
            Add a link
          </label>
          <input id="item-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" autoFocus />
          <input
            aria-label="Link title"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Title (optional)"
          />
          <div className="item-add-actions">
            <button type="button" onClick={() => setAdding(false)}>
              Cancel
            </button>
            <button type="submit" disabled={url.trim() === ""}>
              Add link
            </button>
          </div>
        </form>
      ) : (
        <button type="button" className="item-add-open" onClick={() => setAdding(true)}>
          + Add link
        </button>
      )}
    </aside>
  );
}

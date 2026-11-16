// The item panel of a workspace, at the right of the chat: what was built or
// collected for it, grouped by kind. Every item opens inside the chat screen,
// in the panel (itemTarget.ts): a document or artifact, a page of the viewer,
// or a preview card for an external link. Nothing navigates the chat tab. An
// item with an unsafe address (links.ts) is shown as text. The item that is
// open in the panel is marked in the list.
//
// The list holds one row for each thing the items point at: the same document
// can be recorded twice, as a `plan` and as a `document`, or by a tool and by
// hand, and the panel then shows the row that has a title and opens it
// (`dedupeItems`). A row with no title of its own asks the viewer for the
// title of its document, so no row is labelled with a bare path.
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
import { inertProps, useMedia } from "./viewport";
import {
  addItem,
  listItems,
  pinItem,
  removeItem,
  type WorkspaceItem,
} from "../api/workspaces";
import { getDocumentMeta } from "../api/client";
import { useDocumentPanel } from "./DocumentPanel";
import { isImageItem, itemTarget, targetKey, type ItemTarget } from "./itemTarget";

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

/** The title of a row, or "" when its only label is the path it points at. */
function titleOf(item: WorkspaceItem): string {
  const title = item.title.trim();
  return title && title !== item.ref.trim() ? title : "";
}

// How good a row is as the one row of its target: a title first, then a
// target that opens.
function rank(item: WorkspaceItem): number {
  return (titleOf(item) ? 2 : 0) + (itemTarget(item) ? 1 : 0);
}

/**
 * One row for each thing the items point at (`targetKey`). The same document
 * can be recorded twice, as two kinds or by two writers, and the panel then
 * shows the row that has a title and opens it, never a second row of the same
 * path. Rows of equal worth keep the order they came in.
 */
export function dedupeItems(items: WorkspaceItem[]): WorkspaceItem[] {
  const best = new Map<string, WorkspaceItem>();
  for (const item of items) {
    const key = targetKey(item);
    const kept = best.get(key);
    if (kept === undefined || rank(item) > rank(kept)) best.set(key, item);
  }
  return items.filter((item) => best.get(targetKey(item)) === item);
}

/**
 * What a row is called. A row with no title of its own points at a document
 * with one, so it asks the viewer for it (`<url>.json`) instead of showing
 * the bare path. Until the answer comes, and when it does not come, the slug
 * of the document stands for the title.
 */
function useItemLabel(item: WorkspaceItem, target: ItemTarget | null): string {
  const known = titleOf(item);
  const url = !known && target?.type === "document" ? target.artifact.url : "";
  const [found, setFound] = useState("");
  useEffect(() => {
    setFound("");
    if (!url) return undefined;
    let live = true;
    void getDocumentMeta(url).then((meta) => {
      if (live) setFound(meta?.title.trim() ?? "");
    });
    return () => {
      live = false;
    };
  }, [url]);
  if (known) return known;
  if (found) return found;
  return target?.type === "document" ? target.artifact.slug : label(item);
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
  const text = useItemLabel(item, target);
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
      {isImageItem(item) ? (
        <img className="item-thumb" src={item.url ?? ""} alt="" loading="lazy" />
      ) : (
        <span className="item-icon" aria-hidden="true">
          {KIND_ICONS[item.kind] ?? "•"}
        </span>
      )}
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
  workspace,
  refreshKey,
  open,
  onClose,
  collapsed = false,
  onCollapse,
}: {
  /** The id of the workspace whose items these are, never a slug ref. */
  workspace: string;
  refreshKey: number;
  open: boolean;
  onClose: () => void;
  /** A desktop column that is folded away (index.css). */
  collapsed?: boolean;
  onCollapse?: () => void;
}) {
  // From 1100px the panel is a column; below it, a drawer. One that is not on
  // screen is inert, so its "Add link" fields are out of the keyboard's order.
  const column = useMedia("(min-width: 1100px)");
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
    listItems(workspace)
      .then((rows) => {
        setItems(rows);
        setError("");
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "the items did not load"));
  }, [workspace]);

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
    void act(removeItem(workspace, waiting.item.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace, load]);
  useEffect(
    () => () => {
      const waiting = undoRef.current;
      if (waiting) {
        clearTimeout(waiting.timer);
        void removeItem(workspace, waiting.item.id).catch(() => undefined);
      }
    },
    [workspace],
  );
  const remove = (item: WorkspaceItem) => {
    setMenuFor(null);
    flush();
    const timer = setTimeout(() => {
      setUndo(null);
      void act(removeItem(workspace, item.id));
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
    if (t.type === "image") return panel.view?.type === "image" && panel.view.image.url === t.image.url;
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
    void act(addItem(workspace, { kind: "link", ref: address, title: name.trim(), url: address }));
    setUrl("");
    setName("");
    setAdding(false);
  };

  const shown = dedupeItems(items.filter((item) => item.id !== undo?.item.id));
  const ordered = [...shown].sort((a, b) => Number(b.pinned) - Number(a.pinned));
  return (
    <aside
      id="workspace-items"
      className={`items${open ? " open" : ""}${collapsed ? " collapsed" : ""}`}
      aria-label="Workspace items"
      {...inertProps(column ? collapsed : !open)}
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
                      void act(pinItem(workspace, item.id, !item.pinned));
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

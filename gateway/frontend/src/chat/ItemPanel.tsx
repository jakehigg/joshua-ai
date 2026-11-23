// The item panel of a workspace, at the right of the chat: what was built or
// collected for it, grouped by kind. Every item opens inside the chat screen,
// in the sheet over the workspace (itemTarget.ts, DocumentPanel.tsx): a
// document or artifact, a page of the viewer, or a preview card for an
// external link. Nothing navigates the chat tab. An item with an unsafe
// address (links.ts) is shown as text. The item that is open is marked in the
// list.
//
// The list holds one row for each thing the items point at: the same document
// can be recorded twice, as a `plan` and as a `document`, or by a tool and by
// hand, and the panel then shows the row that has a title and opens it
// (`dedupeItems`). A row with no title of its own asks the viewer for the
// title of its document, so no row is labelled with a bare path.
//
// A row takes two lines at most: a kind icon, the title (cut with an
// ellipsis after the second line, the whole title on hover), a pin mark, and
// a "..." button. The button opens a small
// menu with Pin or Unpin and Remove. Remove hides the row at once and says so
// for a few seconds with an Undo; the call goes up as `removeItem` when the
// time is up. The rows stand pinned first, then the newest added first
// (`orderItems`). A group folds with its header, and the folded groups of each
// workspace are kept per browser in localStorage (try/catch; without it every
// group is open). The form that adds a link is behind "+ Add link".
//
// A long list is narrowed by the filter field at the top: a part of the title
// or of the address, in any case. While it holds text the panel says how many
// rows of how many it shows, and a button empties it.
//
// On a phone the screen closes this list when an item opens, so the sheet of
// the item is the only one over the conversation. The list is a sheet there,
// with a close button. On a wide desktop it is a column that folds away with »
// (the workspace header brings it back), and it keeps its place while an item
// is open (ChatPage.tsx). The list loads on open, after each turn
// (`refreshKey`) and when the tab gets focus back.

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
import { NotesCard } from "./NotesCard";
import type { Workspace } from "../api/workspaces";
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

/**
 * True when `needle`, in lower case, is a part of the title or of the address
 * of `item`. An empty needle matches every row.
 */
export function matchesItem(item: WorkspaceItem, needle: string): boolean {
  if (needle === "") return true;
  return `${item.title} ${item.ref} ${item.url ?? ""}`.toLowerCase().includes(needle);
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

// The folded groups of every workspace: the id of the workspace to the kinds
// folded in it. A group folds in one workspace alone, because the kinds of one
// workspace are not the kinds of another.
type FoldedGroups = Record<string, string[]>;

function readAllGroups(): FoldedGroups {
  const all: FoldedGroups = {};
  try {
    const raw = JSON.parse(window.localStorage.getItem(GROUPS_KEY) ?? "{}") as unknown;
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return all;
    for (const [workspace, kinds] of Object.entries(raw as Record<string, unknown>)) {
      if (Array.isArray(kinds)) {
        all[workspace] = kinds.filter((k): k is string => typeof k === "string");
      }
    }
  } catch {
    // No storage, or a bad value: every group is open.
  }
  return all;
}

function readGroups(workspace: string): string[] {
  return readAllGroups()[workspace] ?? [];
}

function saveGroups(workspace: string, groups: string[]): void {
  try {
    const all = { ...readAllGroups(), [workspace]: groups };
    window.localStorage.setItem(GROUPS_KEY, JSON.stringify(all));
  } catch {
    // Kept in memory only.
  }
}

/** When an item was added, as a number. A row with no stamp is oldest. */
const addedAt = (item: WorkspaceItem): number => {
  const when = Date.parse(item.added_at ?? "");
  return Number.isNaN(when) ? 0 : when;
};

/**
 * The rows in the order the panel shows them: pinned first, then the newest
 * added first. Rows that carry no stamp keep the order the list gave them.
 */
export function orderItems(items: WorkspaceItem[]): WorkspaceItem[] {
  return [...items].sort(
    (a, b) => Number(b.pinned) - Number(a.pinned) || addedAt(b) - addedAt(a),
  );
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
  notes,
}: {
  /** The id of the workspace whose items these are, never a slug ref. */
  workspace: string;
  /** The workspace whose notes stand at the top, and what a change does. */
  notes?: Workspace;
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
  const [filter, setFilter] = useState("");
  const [folded, setFolded] = useState<string[]>(() => readGroups(workspace));
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
    saveGroups(workspace, next);
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

  const here = dedupeItems(items.filter((item) => item.id !== undo?.item.id));
  const needle = filter.trim().toLowerCase();
  const shown = here.filter((item) => matchesItem(item, needle));
  const ordered = orderItems(shown);
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
      {notes && <NotesCard workspace={notes} />}
      {here.length > 0 && (
        <div className="item-filter">
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter"
            aria-label="Filter the items"
          />
          {filter !== "" && (
            <button
              type="button"
              className="item-filter-clear"
              aria-label="Clear the filter"
              onClick={() => setFilter("")}
            >
              ✕
            </button>
          )}
        </div>
      )}
      {needle !== "" && (
        <p className="j-meta item-filter-count" role="status">
          {shown.length} of {here.length}
        </p>
      )}
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
      {shown.length === 0 && !error && (
        <p className="j-meta">{needle === "" ? "Nothing here yet." : "Nothing matches."}</p>
      )}
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

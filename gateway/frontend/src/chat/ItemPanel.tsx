// The item panel of a workspace, at the right of the chat: what was built or
// collected for it, grouped by kind. A document or an artifact opens in the
// document panel. An item with an http(s) or viewer address opens as a link;
// any other address is shown as text (links.ts). Each item can be pinned or
// removed, and a form adds a link. The list loads on open, after each turn
// (`refreshKey`) and when the tab gets focus back.

import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  addItem,
  listItems,
  pinItem,
  removeItem,
  type WorkspaceItem,
} from "../api/workspaces";
import { parseArtifact } from "./ArtifactCard";
import { useDocumentPanel } from "./DocumentPanel";
import { safeHref } from "./links";

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

function Entry({
  item,
  onPin,
  onRemove,
}: {
  item: WorkspaceItem;
  onPin: () => void;
  onRemove: () => void;
}) {
  const panel = useDocumentPanel();
  const doc =
    item.kind === "document" || item.kind === "artifact" ? parseArtifact(item.ref) : null;
  const href = safeHref(item.url);
  let name;
  if (doc) {
    name = (
      <button type="button" className="item-open" onClick={() => panel.show(doc)}>
        {label(item)}
      </button>
    );
  } else if (href) {
    name = (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {label(item)}
      </a>
    );
  } else {
    name = <span className="item-text">{label(item)}</span>;
  }
  return (
    <li className="item">
      {name}
      <span className="item-actions">
        <button
          type="button"
          aria-pressed={item.pinned}
          aria-label={`${item.pinned ? "Unpin" : "Pin"} ${label(item)}`}
          onClick={onPin}
        >
          {item.pinned ? "Unpin" : "Pin"}
        </button>
        <button type="button" aria-label={`Remove ${label(item)}`} onClick={onRemove}>
          Remove
        </button>
      </span>
    </li>
  );
}

export function ItemPanel({
  slug,
  refreshKey,
  open,
  onClose,
}: {
  slug: string;
  refreshKey: number;
  open: boolean;
  onClose: () => void;
}) {
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");

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

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const address = url.trim();
    if (!address) return;
    void act(addItem(slug, { kind: "link", ref: address, title: name.trim(), url: address }));
    setUrl("");
    setName("");
  };

  const ordered = [...items].sort((a, b) => Number(b.pinned) - Number(a.pinned));
  return (
    <aside className={open ? "items open" : "items"} aria-label="Workspace items">
      <header className="items-header">
        <h2>Items</h2>
        <button type="button" className="items-close" onClick={onClose} aria-label="Close the items">
          ✕
        </button>
      </header>
      {error && (
        <p className="j-error" role="alert">
          {error}
        </p>
      )}
      {items.length === 0 && !error && <p className="j-meta">Nothing here yet.</p>}
      {groupItems(ordered).map(([kind, list]) => (
        <section key={kind}>
          <h3 className="rail-label">{KIND_LABELS[kind] ?? kind}</h3>
          <ul>
            {list.map((item) => (
              <Entry
                key={item.id}
                item={item}
                onPin={() => void act(pinItem(slug, item.id, !item.pinned))}
                onRemove={() => void act(removeItem(slug, item.id))}
              />
            ))}
          </ul>
        </section>
      ))}
      <form className="item-add" onSubmit={submit}>
        <label className="j-field-label" htmlFor="item-url">
          Add a link
        </label>
        <input id="item-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" />
        <input
          aria-label="Link title"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Title (optional)"
        />
        <button type="submit" disabled={url.trim() === ""}>
          Add link
        </button>
      </form>
    </aside>
  );
}

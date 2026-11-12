// What the panel shows for a workspace item. The chat tab never navigates: an
// item always opens inside the chat screen, in one of three ways.
//
//   document  a document or an artifact, framed with the reload and watch
//             behaviour of DocumentPanel.
//   page      any other page of the viewer (a same-origin path), framed.
//   card      an address that cannot be framed (an external link, or no
//             address at all): a small preview with an "Open in new tab"
//             button for an http(s) address.
//
// The backend writes a document item with the ref `documents/<owner>/<slug>.md`
// (an artifact: `.html`) and the url `/documents/<owner>/<slug>`. A person can
// also add an item by hand, so the ref or the url alone may be all there is.
// An item whose url is unsafe (javascript:, data:, ...) gets no target, and
// the list shows it as text.

import type { WorkspaceItem } from "../api/workspaces";
import { type Artifact } from "./ArtifactCard";
import { safeHref } from "./links";

export interface PageTarget {
  /** A same-origin path of the viewer, framed by the panel. */
  url: string;
  title: string;
  kindLabel: string;
}

export interface CardTarget {
  title: string;
  kindLabel: string;
  /** What the item points at, shown as text. Empty when nothing is known. */
  address: string;
  /** Set for an http(s) address: the target of "Open in new tab". */
  external: string | null;
}

export type ItemTarget =
  | { type: "document"; artifact: Artifact }
  | { type: "page"; page: PageTarget }
  | { type: "card"; card: CardTarget };

const KIND_NAMES: Record<string, string> = {
  document: "Document",
  artifact: "Artifact",
  plan: "Plan",
  wiki: "Wiki page",
  tasks_project: "Task project",
  task: "Task",
  assignment: "Assignment",
  developer_task: "Developer task",
  pr: "Pull request",
  link: "Link",
  file: "File",
};

export function kindName(kind: string): string {
  return KIND_NAMES[kind] ?? kind;
}

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

// `documents/<owner>/<slug>` with or without a leading `/` and an extension
// (.md, .html), or null.
function documentOf(text: string): Artifact | null {
  const parts = text.trim().replace(/^\//, "").split("?")[0]!.split("/");
  if (parts.length !== 3 || parts[0] !== "documents") return null;
  const owner = parts[1]!;
  const slug = parts[2]!.replace(/\.(md|html)$/, "");
  if (!NAME.test(owner) || !NAME.test(slug)) return null;
  return { path: `documents/${owner}/${slug}.md`, slug, url: `/documents/${owner}/${slug}` };
}

function viewerPath(url: string): boolean {
  return url.startsWith("/");
}

// The viewer page of an item that has no url, when the viewer has one.
function fallbackPath(item: WorkspaceItem): string | null {
  const ref = item.ref.trim();
  if (item.kind === "assignment" && /^\d+$/.test(ref)) return `/assignments/${ref}`;
  if (item.kind === "wiki" && ref.endsWith(".md") && !ref.startsWith("/") && !ref.includes(".."))
    return `/wiki/${ref.split("/").map(encodeURIComponent).join("/")}`;
  return null;
}

export function itemTarget(item: WorkspaceItem): ItemTarget | null {
  const title = item.title.trim() || item.ref;
  const kindLabel = kindName(item.kind);
  const isDoc = item.kind === "document" || item.kind === "artifact";
  const raw = item.url?.trim() ?? "";
  const href = safeHref(raw);
  if (raw && !href) return null;

  const artifact =
    (isDoc ? documentOf(item.ref) : null) ?? (href && viewerPath(href) ? documentOf(href) : null);
  if (artifact) return { type: "document", artifact };

  const path = href && viewerPath(href) ? href : href ? null : fallbackPath(item);
  if (path) return { type: "page", page: { url: path, title, kindLabel } };

  return {
    type: "card",
    card: {
      title,
      kindLabel,
      address: href ?? (item.ref !== title ? item.ref : ""),
      external: href,
    },
  };
}

// The workspace calls: `/api/v1/workspaces`, which the viewer proxies to core.
//
// One workspace is named by its ref, `<space>/<slug>`: `shared/<slug>` for a
// shared workspace, `<owner>/<slug>` for the workspace of a person. `refOf`
// builds it from a workspace and `linkOf` builds the address of its screen.

import { deleteJson, getJson, patchJson, postJson } from "./client";

export type WorkspaceStatus = "active" | "paused" | "archived";
export type Notify = "none" | "questions" | "questions+finishes";

export interface Workspace {
  id: string;
  slug: string;
  title: string;
  owner: string;
  goal: string;
  notes: string;
  servers: string[];
  model: string | null;
  notify: Notify;
  status: WorkspaceStatus;
  // A shared workspace is one thread that several people write in. `members`
  // holds the ids beside the owner, and they never change after the create.
  shared: boolean;
  members: string[];
}

export interface WorkspaceItem {
  id: string;
  kind: string;
  ref: string;
  title: string;
  url: string | null;
  added_by: string;
  pinned: boolean;
  seen: boolean;
}

export interface ServerOption {
  name: string;
  description: string;
}

/** One person a workspace may be shared with. The caller is not in the list. */
export interface PersonOption {
  id: string;
  name: string;
}

/**
 * What a person may pick: the servers they may use, the models, and the
 * people they may share a workspace with.
 */
export interface Options {
  servers: ServerOption[];
  models: string[];
  people: PersonOption[];
}

export interface NewWorkspace {
  title: string;
  goal?: string;
  model?: string;
  servers: string[];
  // The other people of a shared workspace. An empty list is a workspace of
  // the person alone.
  members?: string[];
}

export type WorkspacePatch = Partial<
  Pick<Workspace, "title" | "goal" | "notes" | "servers" | "model" | "notify" | "status">
>;

/** The namespace of a shared workspace, in a ref and in an address. */
export const SHARED = "shared";

/** The namespace of a workspace: `shared`, or the id of its owner. */
export const spaceOf = (workspace: Workspace): string =>
  workspace.shared ? SHARED : workspace.owner;

/**
 * The ref of a workspace, `<space>/<slug>`: what the calls below name it by,
 * and what the chat routes take as `workspace`. A member of a shared
 * workspace reaches it under `shared`, never under a slug of their own.
 */
export const refOf = (workspace: Workspace): string => `${spaceOf(workspace)}/${workspace.slug}`;

/** Where a workspace lives in the viewer: `/chat/w/<space>/<slug>`. */
export const linkOf = (workspace: Workspace): string =>
  `/chat/w/${encodeURIComponent(spaceOf(workspace))}/${encodeURIComponent(workspace.slug)}`;

// The slug core gives a title (core: store/repo.py, `slugify`), so a dialog
// can show the address a workspace will take before it exists.
const SLUG_MAX = 48;
const RESERVED_SLUGS = ["options"];

export function slugOf(title: string): string {
  const trim = (text: string) => text.replace(/^-+|-+$/g, "");
  const slug =
    trim(trim(title.toLowerCase().replace(/[^a-z0-9]+/g, "-")).slice(0, SLUG_MAX)) || "workspace";
  return RESERVED_SLUGS.includes(slug) ? `${slug}-1` : slug;
}

const BASE = "/workspaces";
// A ref with a slash goes URL-encoded; the viewer and core both decode it.
const at = (slug: string) => `${BASE}/${encodeURIComponent(slug)}`;

export async function listWorkspaces(): Promise<Workspace[]> {
  const body = await getJson<{ workspaces?: Workspace[] }>(BASE);
  return Array.isArray(body.workspaces) ? body.workspaces : [];
}

export const getWorkspace = (slug: string) => getJson<Workspace>(at(slug));
export const createWorkspace = (body: NewWorkspace) => postJson<Workspace>(BASE, body);
export const changeWorkspace = (slug: string, body: WorkspacePatch) =>
  patchJson<Workspace>(at(slug), body);
export const getOptions = () => getJson<Options>(`${BASE}/options`);

export async function listItems(slug: string): Promise<WorkspaceItem[]> {
  const body = await getJson<{ items?: WorkspaceItem[] }>(`${at(slug)}/items`);
  return Array.isArray(body.items) ? body.items : [];
}

export const addItem = (
  slug: string,
  item: { kind: string; ref: string; title?: string; url?: string },
) => postJson<WorkspaceItem>(`${at(slug)}/items`, item);
export const pinItem = (slug: string, id: string, pinned: boolean) =>
  patchJson<WorkspaceItem>(`${at(slug)}/items/${encodeURIComponent(id)}`, { pinned });
export const removeItem = (slug: string, id: string) =>
  deleteJson<unknown>(`${at(slug)}/items/${encodeURIComponent(id)}`);

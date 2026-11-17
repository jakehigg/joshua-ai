// The workspace calls: `/api/v1/workspaces`, which the viewer proxies to core.
//
// Every call that names one workspace names it by its id, and so do the chat
// turn and history calls. The id is the one name of a row: a closed workspace
// keeps its slug and frees its name, so a ref `<space>/<slug>` may move to
// another workspace, and a viewer that sent a ref would read and write the row
// that took the name. The ref is the human-readable form, for a person to read
// and for an address a person types; `linkOf` builds the address of the screen
// of a workspace, which is its id.

import { deleteJson, getJson, patchJson, postJson } from "./client";

export type WorkspaceStatus = "active" | "paused" | "archived";
export type Notify = "none" | "questions" | "questions+finishes";
/** How Joshua writes in a workspace: the house voice, STE, or a coach. */
export type Style = "none" | "ste" | "coach";

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
  style: Style;
  status: WorkspaceStatus;
  // A shared workspace is one thread that several people write in. `members`
  // holds the ids beside the owner, and they never change after the create.
  shared: boolean;
  members: string[];
  // How many rows of the thread the signed-in person has not read. Only the
  // list gives it, and it is the count of that person alone: in a shared
  // workspace every other person has one of their own.
  unread?: number;
  // Whether Joshua is working for this workspace, or something of it waits
  // for a person. Null, or absent, when neither is true. The viewer composes
  // it (`gateway/joshua_gateway/viewer_workspaces.py`); the list and the read
  // of one workspace give it.
  charm?: Charm | null;
}

/** Joshua is working for the workspace, or it waits for a person. */
export type CharmState = "working" | "waiting";

/** The charm beside the name of a workspace, with its one line of why. */
export interface Charm {
  state: CharmState;
  reason: string;
}

/**
 * When Joshua may reach one person of a shared workspace, which governs the
 * `notify_user` tool: `off` never, `decide` when the thread needs them, `all`
 * on every message another person writes.
 */
export type NotifyMode = "off" | "decide" | "all";

/** The notify mode of the signed-in person, and the default of the workspace. */
export interface NotifyModeState {
  mode: NotifyMode;
  default: NotifyMode;
}

/** How far a person has read a thread, as the read call answers. */
export interface ReadMark {
  last_read_turn_id: number;
  unread: number;
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

/** One writing style a workspace may take, with the line the picker shows. */
export interface StyleOption {
  id: Style;
  name: string;
  description: string;
}

/** One person a workspace may be shared with. The caller is not in the list. */
export interface PersonOption {
  id: string;
  name: string;
}

/**
 * What a person may pick: the servers they may use, the models, the writing
 * styles, and the people they may share a workspace with.
 */
export interface Options {
  servers: ServerOption[];
  models: string[];
  styles: StyleOption[];
  people: PersonOption[];
}

export interface NewWorkspace {
  title: string;
  goal?: string;
  model?: string;
  style?: Style;
  servers: string[];
  // The other people of a shared workspace. An empty list is a workspace of
  // the person alone.
  members?: string[];
}

export type WorkspacePatch = Partial<
  Pick<
    Workspace,
    "title" | "goal" | "notes" | "servers" | "model" | "notify" | "style" | "status"
  >
>;

/** The namespace of a shared workspace, in a ref and in an address. */
export const SHARED = "shared";

/**
 * Where a workspace lives in the viewer: `/chat/w/<id>`, its permanent
 * address. A closed workspace keeps its slug but frees the name, so a later
 * workspace may hold `/chat/w/<space>/<slug>`; the id never moves. A slug
 * address still opens the workspace and goes to the id (ChatPage.tsx).
 */
export const linkOf = (workspace: Workspace): string =>
  `/chat/w/${encodeURIComponent(workspace.id)}`;

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
// One workspace, by its id. Core's route takes a ref, and an id is one of the
// forms it takes; the id holds no slash, and goes encoded all the same.
const at = (id: string) => `${BASE}/${encodeURIComponent(id)}`;

export async function listWorkspaces(): Promise<Workspace[]> {
  const body = await getJson<{ workspaces?: Workspace[] }>(BASE);
  return Array.isArray(body.workspaces) ? body.workspaces : [];
}

/**
 * One workspace, by its id. The screen resolves the address it was opened at
 * once, here, and every call after it takes `workspace.id`; so this is also
 * where a ref a person typed (`<space>/<slug>`, or a bare slug) is resolved,
 * and the screen then goes to the id (ChatPage.tsx).
 */
export const getWorkspace = (id: string) => getJson<Workspace>(at(id));
export const createWorkspace = (body: NewWorkspace) => postJson<Workspace>(BASE, body);
export const changeWorkspace = (id: string, body: WorkspacePatch) =>
  patchJson<Workspace>(at(id), body);
export const getOptions = () => getJson<Options>(`${BASE}/options`);

/**
 * Mark the thread of a workspace read up to a row id, for the signed-in
 * person. The mark is theirs alone: in a shared workspace every other person
 * keeps the count they have.
 */
export const markRead = (id: string, turnId: number) =>
  postJson<ReadMark>(`${at(id)}/read`, { turn_id: turnId });

/**
 * The notify mode of the signed-in person in a workspace, and the write of it.
 * The mode is theirs alone: nobody sets the mode of another person.
 */
export const getNotifyMode = (id: string) => getJson<NotifyModeState>(`${at(id)}/notify`);
export const setNotifyMode = (id: string, mode: NotifyMode) =>
  postJson<NotifyModeState>(`${at(id)}/notify`, { mode });

export async function listItems(id: string): Promise<WorkspaceItem[]> {
  const body = await getJson<{ items?: WorkspaceItem[] }>(`${at(id)}/items`);
  return Array.isArray(body.items) ? body.items : [];
}

export const addItem = (
  id: string,
  item: { kind: string; ref: string; title?: string; url?: string },
) => postJson<WorkspaceItem>(`${at(id)}/items`, item);
export const pinItem = (id: string, itemId: string, pinned: boolean) =>
  patchJson<WorkspaceItem>(`${at(id)}/items/${encodeURIComponent(itemId)}`, { pinned });
export const removeItem = (id: string, itemId: string) =>
  deleteJson<unknown>(`${at(id)}/items/${encodeURIComponent(itemId)}`);

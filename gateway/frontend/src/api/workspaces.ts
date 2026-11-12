// The workspace calls: `/api/v1/workspaces`, which the viewer proxies to core.

import { deleteJson, getJson, patchJson, postJson } from "./client";

export type WorkspaceStatus = "active" | "paused" | "archived";
export type Notify = "none" | "questions" | "questions+finishes";

export interface Workspace {
  id: string;
  slug: string;
  title: string;
  goal: string;
  notes: string;
  servers: string[];
  model: string | null;
  notify: Notify;
  status: WorkspaceStatus;
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

/** What a person may pick: the servers they may use and the models. */
export interface Options {
  servers: ServerOption[];
  models: string[];
}

export interface NewWorkspace {
  title: string;
  goal?: string;
  model?: string;
  servers: string[];
}

export type WorkspacePatch = Partial<
  Pick<Workspace, "title" | "goal" | "notes" | "servers" | "model" | "notify" | "status">
>;

const BASE = "/workspaces";
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

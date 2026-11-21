// The workspaces of the person, for the list in the app sidebar and for the
// chat screen.
//
// The list is one store, so the sidebar and the chat screen share one call and
// one answer: a workspace that is created, or a thread that is marked read,
// reloads both. The store is empty again when nothing reads it, so the next
// screen starts from the server.

import { useCallback, useSyncExternalStore } from "react";
import { listWorkspaces, type Workspace } from "../api/workspaces";

export interface Workspaces {
  list: Workspace[];
  reload: () => void;
}

let rows: Workspace[] = [];
const listeners = new Set<() => void>();

function announce(): void {
  listeners.forEach((l) => l());
}

function load(): void {
  listWorkspaces()
    .then((found) => {
      rows = found;
      announce();
    })
    .catch(() => undefined); // The list shows what it has; the chat works without it.
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) load();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) rows = [];
  };
}

const snapshot = () => rows;

export function useWorkspaces(): Workspaces {
  const list = useSyncExternalStore(subscribe, snapshot, snapshot);
  const reload = useCallback(() => load(), []);
  return { list, reload };
}

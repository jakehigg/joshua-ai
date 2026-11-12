// Which side columns are collapsed on a desktop: the app sidebar (`nav`), the
// rail of the chat (`rail`) and the item column of a workspace (`items`).
//
// The state is shared by the shell and the chat screen, so a collapsed
// sidebar stays collapsed on every page, and it is kept per browser in
// localStorage. Every access to the storage is in try/catch, and without it
// the state lives in memory only. On a phone these columns are drawers with
// their own state, and nothing here changes them (the classes this state
// adds only act from the desktop breakpoints in index.css).

import { useCallback, useSyncExternalStore } from "react";

export type Panel = "nav" | "rail" | "items";

export const PANELS: Panel[] = ["nav", "rail", "items"];
export const STORAGE_KEY = "joshua.panels.collapsed";

type State = Record<Panel, boolean>;

function read(): State {
  const state: State = { nav: false, rail: false, items: false };
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") as Partial<State>;
    for (const p of PANELS) state[p] = raw[p] === true;
  } catch {
    // No storage, or a bad value: every panel is open.
  }
  return state;
}

let state: State = read();
const listeners = new Set<() => void>();

function write(next: State): void {
  state = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // The state stays in memory.
  }
  listeners.forEach((l) => l());
}

/** Read the storage again. A test uses it after it seeds the storage. */
export function reloadPanels(): void {
  state = read();
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

const snapshot = () => state;

export function usePanels() {
  const now = useSyncExternalStore(subscribe, snapshot, snapshot);
  const set = useCallback((panel: Panel, collapsed: boolean) => write({ ...state, [panel]: collapsed }), []);
  const toggle = useCallback((panel: Panel) => write({ ...state, [panel]: !state[panel] }), []);
  /** Focus mode: collapse every panel, or open them all when all are collapsed. */
  const focus = useCallback(() => {
    const all = PANELS.every((p) => state[p]);
    write({ nav: !all, rail: !all, items: !all });
  }, []);
  return { collapsed: now, set, toggle, focus };
}

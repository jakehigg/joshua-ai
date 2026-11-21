// How wide a person dragged a panel of the chat: the items column of a
// workspace (`items`) and the document panel (`doc`).
//
// A layout width is the choice of one device, so it is kept per browser in
// localStorage and never on the account. Every access to the storage is in
// try/catch, and without it the width lives in memory only.
//
// The token of the stylesheet is the default: a panel that was never dragged,
// and one that a double-click on its handle reset, is as wide as
// --j-panel-items or --j-panel-doc says (joshua-ui.css). A width that is kept
// comes to the chat screen as a custom property, which the stylesheet takes
// over the token. A drag is clamped to the limits below, so a panel can
// neither take the screen nor disappear, and the thread keeps room.

import { useCallback, useSyncExternalStore, type CSSProperties } from "react";

export type Panel = "items" | "doc";

export const PANELS: Panel[] = ["items", "doc"];
export const STORAGE_KEY = "joshua.panels.widths";

/** How narrow and how wide each panel goes, in rem. */
export const LIMITS: Record<Panel, { min: number; max: number }> = {
  items: { min: 12, max: 32 },
  doc: { min: 22, max: 56 },
};

type State = Partial<Record<Panel, number>>;

function read(): State {
  const state: State = {};
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") as Partial<
      Record<Panel, unknown>
    >;
    for (const panel of PANELS) {
      const width = raw[panel];
      if (typeof width === "number" && Number.isFinite(width) && width > 0) {
        state[panel] = Math.round(width);
      }
    }
  } catch {
    // No storage, or a bad value: every panel follows its token.
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
    // The widths stay in memory.
  }
  listeners.forEach((l) => l());
}

/** Read the storage again. A test uses it after it seeds the storage. */
export function reloadWidths(): void {
  state = read();
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const snapshot = () => state;

/** A length in rem as pixels, in the root font size of the browser. */
export function remPx(rem: number): number {
  const root = parseFloat(window.getComputedStyle(document.documentElement).fontSize);
  return rem * (Number.isFinite(root) && root > 0 ? root : 16);
}

/** `px`, held inside the limits of `panel`. */
export function clampWidth(panel: Panel, px: number): number {
  const { min, max } = LIMITS[panel];
  return Math.round(Math.min(remPx(max), Math.max(remPx(min), px)));
}

export interface PanelWidths {
  /** The width of each panel in px, or nothing where the token holds. */
  width: State;
  set: (panel: Panel, px: number) => void;
  reset: (panel: Panel) => void;
  /** What the chat screen carries: one custom property per kept width. */
  style: CSSProperties;
}

export function usePanelWidths(): PanelWidths {
  const now = useSyncExternalStore(subscribe, snapshot, snapshot);
  const set = useCallback(
    (panel: Panel, px: number) => write({ ...state, [panel]: clampWidth(panel, px) }),
    [],
  );
  const reset = useCallback((panel: Panel) => {
    const next = { ...state };
    delete next[panel];
    write(next);
  }, []);
  const style: Record<string, string> = {};
  for (const panel of PANELS) {
    const width = now[panel];
    if (width !== undefined) style[`--${panel}-w`] = `${width}px`;
  }
  return { width: now, set, reset, style: style as CSSProperties };
}

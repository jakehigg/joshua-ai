// How wide a person dragged a panel of the chat: the items column of a
// workspace (`items`), the document panel (`doc`), and the sheet of a
// workspace item (`sheet`).
//
// A layout width is the choice of one device, so it is kept per browser in
// localStorage and never on the account. One browser holds one width for each
// person, each panel and each breakpoint (`keyOf`): the same laptop shows the
// items column of one person as wide as they left it, a phone keeps its own
// width, and a window that grows into another breakpoint takes the width the
// person gave that breakpoint. Every access to the storage is in try/catch,
// and without it the widths live in memory only.
//
// The token of the stylesheet is the default: a panel that was never dragged,
// and one that a double-click on its handle reset, is as wide as
// --j-panel-items or --j-panel-doc says (joshua-ui.css), or --panel-sheet
// (index.css). A width that is kept
// comes to the chat screen as a custom property, which the stylesheet takes
// over the token. A drag is clamped to the limits below, so a panel holds
// between MIN_REM and MAX_SHARE of the window and the thread keeps room. The
// sheet stands over the thread, not beside it, so it may take SHEET_MAX_SHARE.
//
// Whose widths these are comes from `PanelWidthsPerson`, which the chat screen
// puts around the conversation. With no person named, the widths are those of
// the browser alone.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type CSSProperties,
} from "react";

export type Panel = "items" | "doc" | "sheet";

export const PANELS: Panel[] = ["items", "doc", "sheet"];
export const STORAGE_KEY = "joshua.panels.widths";

/** How narrow a panel goes, in rem. */
export const MIN_REM = 14;
/** How much of the window a panel takes at most. */
export const MAX_SHARE = 0.6;
/** How much of the window the sheet of a workspace item takes at most. */
export const SHEET_MAX_SHARE = 0.96;

/**
 * The breakpoints of the chat row, as index.css draws them: a phone, where a
 * panel is a sheet or a drawer, a window wide enough for the document panel
 * beside the conversation, and one wide enough for the items column too.
 */
export type Breakpoint = "phone" | "wide" | "widest";

const DOC_COLUMN_PX = 900;
const ITEMS_COLUMN_PX = 1100;

/** Which breakpoint a window width is in. */
export function breakpointOf(width: number): Breakpoint {
  if (width >= ITEMS_COLUMN_PX) return "widest";
  return width >= DOC_COLUMN_PX ? "wide" : "phone";
}

/** The breakpoint the window is in now, as state: a resize changes it. */
function useBreakpoint(): Breakpoint {
  const [breakpoint, setBreakpoint] = useState(() => breakpointOf(window.innerWidth));
  useEffect(() => {
    const onResize = () => setBreakpoint(breakpointOf(window.innerWidth));
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return breakpoint;
}

/** The id of the person whose widths these are; "" outside a chat screen. */
const PersonContext = createContext("");

/** The chat screen names the signed-in person for the widths under it. */
export const PanelWidthsPerson = PersonContext.Provider;

// One kept width: whose it is, which panel, and at which breakpoint.
export const keyOf = (person: string, breakpoint: Breakpoint, panel: Panel): string =>
  `${person}|${breakpoint}|${panel}`;

type State = Record<string, number>;

function read(): State {
  const state: State = {};
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<
      string,
      unknown
    >;
    for (const [key, width] of Object.entries(raw)) {
      if (typeof width === "number" && Number.isFinite(width) && width > 0) {
        state[key] = Math.round(width);
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

/** `px`, held between MIN_REM and the largest share of the window for `panel`. */
export function clampWidth(px: number, panel: Panel = "doc"): number {
  const min = remPx(MIN_REM);
  const share = panel === "sheet" ? SHEET_MAX_SHARE : MAX_SHARE;
  const max = Math.max(min, window.innerWidth * share);
  return Math.round(Math.min(max, Math.max(min, px)));
}

export interface PanelWidths {
  /** The width of each panel in px, or nothing where the token holds. */
  width: Partial<Record<Panel, number>>;
  set: (panel: Panel, px: number) => void;
  reset: (panel: Panel) => void;
  /** What the chat screen carries: one custom property per kept width. */
  style: CSSProperties;
}

export function usePanelWidths(): PanelWidths {
  const now = useSyncExternalStore(subscribe, snapshot, snapshot);
  const person = useContext(PersonContext);
  const breakpoint = useBreakpoint();
  const set = useCallback(
    (panel: Panel, px: number) =>
      write({ ...state, [keyOf(person, breakpoint, panel)]: clampWidth(px, panel) }),
    [person, breakpoint],
  );
  const reset = useCallback(
    (panel: Panel) => {
      const next = { ...state };
      delete next[keyOf(person, breakpoint, panel)];
      write(next);
    },
    [person, breakpoint],
  );
  const width: Partial<Record<Panel, number>> = {};
  const style: Record<string, string> = {};
  for (const panel of PANELS) {
    const px = now[keyOf(person, breakpoint, panel)];
    if (px !== undefined) {
      width[panel] = px;
      style[`--${panel}-w`] = `${px}px`;
    }
  }
  return { width, set, reset, style: style as CSSProperties };
}

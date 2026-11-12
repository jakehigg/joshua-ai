// Three small helpers for a phone: the keyboard, the media query, and `inert`.
//
// useKeyboardFit: iOS Safari does not shrink the page when the keyboard opens.
// It keeps the layout height and slides the visual viewport (the part that
// shows) over it, so a composer at the bottom ends up under the keyboard. This
// hook reads window.visualViewport, on its resize and scroll events and when a
// field takes the focus, and sets on <html>:
//   --vvh       the visible height, in px
//   --vv-top    how far the visible part is from the top of the page, in px
//   --kb-inset  the height of the keyboard, in px
//   class vv-fit  (always, while the hook runs) and kb-open (keyboard up)
// index.css then pins the chat shell to that box (position fixed, the visible
// top and height), so the header stays at the top, the list scrolls, and the
// composer sits on the keyboard. The page itself cannot scroll under it. A
// browser with no visualViewport gets none of this and keeps the 100dvh layout.
//
// useMedia: a media query as state, for the drawers that are columns on a wide
// screen. Without matchMedia it answers `fallback` (a wide screen).
//
// inertProps: what a closed drawer spreads on its element. An `inert` element
// has no focusable field, so the "next field" arrow of the iOS keyboard goes
// only to a field that is on screen. React 18 has no `inert` prop, so the
// attribute is written out.

import { useEffect, useState } from "react";

// A keyboard is at least this tall; a smaller change is the browser's toolbar.
const KEYBOARD_MIN_PX = 80;

export function useKeyboardFit(): void {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return undefined;
    const root = document.documentElement;
    const apply = () => {
      const inset = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      root.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
      root.style.setProperty("--vv-top", `${Math.round(vv.offsetTop)}px`);
      root.style.setProperty("--kb-inset", `${inset}px`);
      root.classList.toggle("kb-open", inset > KEYBOARD_MIN_PX);
    };
    // iOS scrolls the document to bring a focused field up. The shell is
    // pinned to the visible box, so put the document back and measure again,
    // at once and after the keyboard has finished opening.
    const settle = () => {
      window.scrollTo(0, 0);
      apply();
    };
    const timers: ReturnType<typeof setTimeout>[] = [];
    const onFocus = (e: FocusEvent) => {
      if (!(e.target instanceof HTMLElement) || !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      settle();
      timers.push(setTimeout(settle, 120), setTimeout(settle, 350));
    };
    root.classList.add("vv-fit");
    apply();
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
    document.addEventListener("focusin", onFocus);
    return () => {
      vv.removeEventListener("resize", apply);
      vv.removeEventListener("scroll", apply);
      document.removeEventListener("focusin", onFocus);
      timers.forEach(clearTimeout);
      root.classList.remove("vv-fit", "kb-open");
      for (const name of ["--vvh", "--vv-top", "--kb-inset"]) root.style.removeProperty(name);
    };
  }, []);
}

export function useMedia(query: string, fallback = true): boolean {
  const read = () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : fallback);
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const list = window.matchMedia(query);
    const onChange = () => setMatches(list.matches);
    onChange();
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

// Spread on a drawer, a sheet or a panel that is not on screen.
export function inertProps(closed: boolean): Record<string, unknown> {
  return closed ? { inert: "", "aria-hidden": true } : {};
}

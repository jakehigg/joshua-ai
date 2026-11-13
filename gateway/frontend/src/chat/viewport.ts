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
//   --kb-extra  room to add under the composer while the keyboard is up: what
//               a measure finds hanging below the visible box, or on iOS an
//               allowance (44px, `?kbpad=N` sets it) for the accessory bar
//   class vv-fit  (always, while the hook runs) and kb-open (keyboard up)
// index.css then pins the chat shell to that box (position fixed, the visible
// top and height), so the header stays at the top, the list scrolls, and the
// composer sits on the keyboard. The page itself cannot scroll under it. On
// focus the hook also brings the field that has the focus to the middle of
// what shows, after the keyboard has opened, so the caret is never behind it.
// A browser with no visualViewport gets none of this and keeps the 100dvh
// layout.
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

// The iOS form accessory bar (the arrows and the check mark) is about this
// tall, and no viewport number counts it reliably.
const IOS_ACCESSORY_PX = 44;
// Extra room above a composer that the measure found low, in px.
const OVERLAP_MARGIN_PX = 4;

// iPhone, iPad, and iPadOS that reports itself as a Mac with a touch screen.
export function isIosWebKit(): boolean {
  const ua = navigator.userAgent || "";
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  return ios && /AppleWebKit/.test(ua);
}

// The allowance for the accessory bar. `?kbpad=60` sets it, and it stays for
// the session so the value survives a move inside the app.
function accessoryAllowance(): number {
  let raw: string | null = null;
  try {
    raw = new URLSearchParams(window.location.search).get("kbpad");
    if (raw !== null) sessionStorage.setItem("kbpad", raw);
    else raw = sessionStorage.getItem("kbpad");
  } catch {
    /* no storage: use the query alone */
  }
  const n = raw === null || raw === "" ? NaN : Number(raw);
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : IOS_ACCESSORY_PX;
}

function debugOn(): boolean {
  try {
    if (new URLSearchParams(window.location.search).get("debug") === "vv") sessionStorage.setItem("debug-vv", "1");
    return sessionStorage.getItem("debug-vv") === "1";
  } catch {
    return false;
  }
}

// A small readout of the numbers, for a phone with no dev tools. It has no
// style sheet, and it ignores touches.
function startDebug(vv: VisualViewport): () => void {
  const box = document.createElement("pre");
  box.setAttribute("data-vv-debug", "");
  box.style.cssText =
    "position:fixed;top:0;left:0;right:0;z-index:2147483647;margin:0;padding:2px 4px;font:10px/1.2 monospace;" +
    "color:#0f0;background:rgba(0,0,0,.75);pointer-events:none;white-space:pre-wrap";
  document.body.appendChild(box);
  const r = (el: Element | null) => {
    if (!el) return "none";
    const b = el.getBoundingClientRect();
    return `${Math.round(b.top)}..${Math.round(b.bottom)}`;
  };
  const root = document.documentElement;
  const draw = () => {
    box.textContent =
      `inner ${window.innerHeight} outer ${window.outerHeight} screen ${screen.height} client ${root.clientHeight}\n` +
      `vv h ${vv.height} top ${vv.offsetTop} page ${vv.pageTop} scale ${vv.scale}\n` +
      `kb-inset ${root.style.getPropertyValue("--kb-inset")} kb-extra ${root.style.getPropertyValue("--kb-extra")} ` +
      `kb-open ${root.classList.contains("kb-open")}\n` +
      `composer ${r(document.querySelector(".chat-screen .composer"))} shell ${r(document.querySelector(".j-shell.fill"))} ` +
      `scrollTop ${document.scrollingElement?.scrollTop ?? "?"}`;
  };
  draw();
  vv.addEventListener("resize", draw);
  vv.addEventListener("scroll", draw);
  const id = setInterval(draw, 250);
  return () => {
    vv.removeEventListener("resize", draw);
    vv.removeEventListener("scroll", draw);
    clearInterval(id);
    box.remove();
  };
}

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
      const open = inset > KEYBOARD_MIN_PX;
      root.classList.toggle("kb-open", open);
      // The shell carries --kb-extra as bottom padding (index.css). Measure
      // the composer as if it had none, then lift it by what still hangs
      // below the visible box, and by at least the accessory allowance on iOS.
      let extra = 0;
      if (open) {
        const composer = document.querySelector(".chat-screen .composer");
        if (composer) {
          const was = parseFloat(root.style.getPropertyValue("--kb-extra")) || 0;
          const over = composer.getBoundingClientRect().bottom + was - (vv.offsetTop + vv.height);
          if (over > 0) extra = Math.ceil(over) + OVERLAP_MARGIN_PX;
        }
        if (isIosWebKit()) extra = Math.max(extra, accessoryAllowance());
      }
      root.style.setProperty("--kb-extra", `${extra}px`);
    };
    // iOS scrolls the document to bring a focused field up. The shell is
    // pinned to the visible box, so put the document back and measure again,
    // at once and after the keyboard has finished opening. Each later pass
    // also brings the field itself to the middle of what shows, once the
    // viewport has resized: the composer of the chat, and the caret in it,
    // then stand above the keyboard and not behind it.
    const settle = (field?: HTMLElement) => {
      window.scrollTo(0, 0);
      apply();
      field?.scrollIntoView?.({ block: "center" });
    };
    const timers: ReturnType<typeof setTimeout>[] = [];
    const onFocus = (e: FocusEvent) => {
      if (!(e.target instanceof HTMLElement) || !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      const field = e.target;
      settle();
      timers.push(setTimeout(() => settle(field), 120), setTimeout(() => settle(field), 350), setTimeout(() => settle(field), 800));
    };
    root.classList.add("vv-fit");
    apply();
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
    document.addEventListener("focusin", onFocus);
    const stopDebug = debugOn() ? startDebug(vv) : undefined;
    return () => {
      stopDebug?.();
      vv.removeEventListener("resize", apply);
      vv.removeEventListener("scroll", apply);
      document.removeEventListener("focusin", onFocus);
      timers.forEach(clearTimeout);
      root.classList.remove("vv-fit", "kb-open");
      for (const name of ["--vvh", "--vv-top", "--kb-inset", "--kb-extra"]) root.style.removeProperty(name);
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

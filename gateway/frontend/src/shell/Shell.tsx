import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

import { getJson, type Person } from "../api/client";
import { usePanels } from "./panels";
import AppNav, { ME_SECTION, NavContext, type SessionSection } from "./AppNav";
import { LINKS_SECTION, SECTIONS } from "./sections";

export { ME_SECTION, type SessionSection };

/** The answer of `GET /api/v1/session`. */
export interface Session {
  person: Person;
  anonymous: boolean;
  sections: SessionSection[];
  chat_url: string;
  timezone: string;
}

/**
 * The session the app has already read, for the shell under it.
 *
 * `App` reads `GET /api/v1/session` once, to know whether anybody is signed
 * in at all, and puts the answer here, so the shell draws the navigation
 * without asking a second time. Null means that nobody read it, and the
 * shell then reads it itself.
 */
export const SessionContext = createContext<Session | null>(null);

const LABELS: Record<string, string> = Object.fromEntries(
  SECTIONS.map((item) => [item.key, item.label]),
);

function title(section: string): string {
  if (section === ME_SECTION) {
    return "Profile";
  }
  if (section === LINKS_SECTION) {
    return "Links";
  }
  // The admin area is no section of the session: the owner reaches it by its
  // address, and the app bar names it all the same.
  if (section === "admin") {
    return "Admin";
  }
  return LABELS[section] ?? "Home";
}

/**
 * True when this app runs in a frame of another app, or when the address
 * carries `?embed=1`. `view` is the window, which a test gives.
 *
 * The chat frames a document page in its artifact panel, so the page there
 * is the document and nothing else. Reading `top` of a frame of another
 * origin throws, and that is a frame too.
 */
export function embedded(view: Window = window): boolean {
  try {
    if (new URLSearchParams(view.location.search).get("embed") === "1") {
      return true;
    }
    return view.self !== view.top;
  } catch {
    return true;
  }
}

/** The class of the content column: the usage page is wide, the chat fills. */
function contentClass(section: string): string {
  if (section === "usage") {
    return "j-content wide";
  }
  return section === "chat" ? "j-content fill" : "j-content";
}

/**
 * The shell of every page: the sidebar on a wide screen; on a phone one
 * compact app bar (hamburger, logo, page title) and the same navigation as a
 * drawer on the left. The hamburger opens the drawer over a dimmed backdrop.
 * A tap on the backdrop, the x, Escape and any navigation close it; while it
 * is open the page behind does not scroll, focus goes into it, and comes back
 * to the hamburger when it closes. `section` marks the current item.
 *
 * On a desktop the sidebar can be collapsed with « and opened again with »
 * in the app bar (panels.ts keeps the choice per browser, for every page).
 * There is no search field in the app bar: the Search link of the navigation
 * opens the search page, which holds the field.
 *
 * The chat has its own compact top bar on a phone (the chat header, with the
 * hamburger of its rail, which holds the workspaces and these same links), so
 * the app bar is not drawn on the chat page at any width, and on a phone
 * there is one toggle.
 *
 * In a frame there is no navigation at all: no sidebar and no app bar, so the
 * page of the frame is the content and the person navigates in the app that
 * holds the frame.
 */
export default function Shell({
  section = "",
  children,
}: {
  section?: string;
  children: ReactNode;
}) {
  const given = useContext(SessionContext);
  const [read, setRead] = useState<Session | null>(null);
  const session = given ?? read;
  const frame = embedded();
  const [open, setOpen] = useState(false);
  const panels = usePanels();
  const navCollapsed = panels.collapsed.nav;
  const toggleRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  const { pathname } = useLocation();

  // A route change closes the drawer.
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (open) {
      closeRef.current?.focus();
      wasOpen.current = true;
    } else if (wasOpen.current) {
      wasOpen.current = false;
      toggleRef.current?.focus();
    }
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.documentElement.classList.add("j-lock");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.documentElement.classList.remove("j-lock");
    };
  }, [open]);

  useEffect(() => {
    if (frame || given !== null) {
      return;
    }
    let live = true;
    getJson<Session>("/session")
      .then((answer) => {
        if (live) {
          setRead(answer);
        }
      })
      .catch(() => {
        // The navigation is not the page: with no session the page still
        // shows its own body and its own error.
      });
    return () => {
      live = false;
    };
  }, [frame, given]);

  if (frame) {
    return (
      <div className="j-shell">
        <div className="j-main">
          <main className="j-content">{children}</main>
        </div>
      </div>
    );
  }

  const sections = session?.sections ?? [];
  const owner = session?.person.role === "owner";
  // The chat fills the screen: its own column scrolls, and the page does not.
  const fill = section === "chat";
  return (
    <NavContext.Provider value={{ sections, section, owner }}>
      <div
        className={`j-shell has-sidebar${fill ? " fill" : ""}${navCollapsed ? " nav-collapsed" : ""}`}
      >
        <aside
          className={open ? "j-sidebar open" : "j-sidebar"}
          id="app-nav"
          aria-label="Navigation"
          onClick={(e) => {
            if ((e.target as HTMLElement).closest("a")) setOpen(false);
          }}
        >
          <div className="j-sidebar-head">
            <a className="j-brand" href="/">
              <img className="j-logo" src="/logo-64.png" alt="" />
              Joshua
            </a>
            <button
              type="button"
              ref={closeRef}
              className="j-nav-close"
              aria-label="Close the navigation"
              onClick={() => setOpen(false)}
            >
              ✕
            </button>
            <button
              type="button"
              className="j-nav-collapse"
              aria-label="Hide the navigation"
              aria-expanded={!navCollapsed}
              aria-controls="app-nav"
              onClick={() => panels.set("nav", true)}
            >
              «
            </button>
          </div>
          <AppNav sections={sections} section={section} owner={owner} />
        </aside>
        {open && <div className="j-scrim" data-testid="nav-backdrop" onClick={() => setOpen(false)} />}
        <div className="j-main">
          <header className="j-appbar">
            {!fill && (
              <button
                type="button"
                ref={toggleRef}
                className="j-nav-toggle"
                aria-label="Show the navigation"
                aria-expanded={open}
                aria-controls="app-nav"
                onClick={() => setOpen((o) => !o)}
              >
                ☰
              </button>
            )}
            <button
              type="button"
              className="j-nav-expand"
              aria-label="Open the navigation column"
              aria-expanded={!navCollapsed}
              aria-controls="app-nav"
              onClick={() => panels.set("nav", false)}
            >
              »
            </button>
            <img className="j-logo" src="/logo-64.png" alt="" />
            <a className="j-appbar-title" href="/">
              <small>Joshua</small>
              <strong>{title(section)}</strong>
            </a>
          </header>
          <main className={contentClass(section)}>{children}</main>
        </div>
      </div>
    </NavContext.Provider>
  );
}

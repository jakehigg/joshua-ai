import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { getJson, type Person } from "../api/client";
import Icon from "./Icon";
import LinksNav from "./LinksNav";
import SearchField from "./SearchField";
import { LINKS_PATH, LINKS_SECTION, ME_ICON, SECTIONS, section as sectionOf } from "./sections";

/** One section of the navigation, as `GET /api/v1/session` sends it. */
export interface SessionSection {
  key: string;
  label: string;
  path: string;
}

/** The answer of `GET /api/v1/session`. */
export interface Session {
  person: Person;
  anonymous: boolean;
  sections: SessionSection[];
  chat_url: string;
  timezone: string;
}

/** The key of the item of "Your page". */
export const ME_SECTION = "me";

const LABELS: Record<string, string> = Object.fromEntries(
  SECTIONS.map((item) => [item.key, item.label]),
);

function title(section: string): string {
  if (section === ME_SECTION) {
    return "You";
  }
  if (section === LINKS_SECTION) {
    return "Links";
  }
  return LABELS[section] ?? "Home";
}

function current(key: string, section: string): "page" | undefined {
  return key === section ? "page" : undefined;
}

/**
 * One item of the navigation. A section this app renders is a router link; a
 * section the server still renders is a plain link, so the browser loads the
 * page.
 */
function NavItem({
  path,
  migrated,
  className,
  mark,
  children,
}: {
  path: string;
  migrated: boolean;
  className: string;
  mark: "page" | undefined;
  children: ReactNode;
}) {
  if (migrated) {
    return (
      <Link className={className} to={path} aria-current={mark}>
        {children}
      </Link>
    );
  }
  return (
    <a className={className} href={path} aria-current={mark}>
      {children}
    </a>
  );
}

function isMigrated(key: string): boolean {
  return sectionOf(key)?.migrated ?? false;
}

function iconOf(key: string): string {
  return sectionOf(key)?.icon ?? "";
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
 * The shell of every page: the sidebar on a wide screen, the app bar and the
 * tabs on a phone. `section` marks the current item. The search field is in
 * the app bar, so it shows on every screen; it goes to `/search`, a page of
 * this app. `query` is what the field holds, which the search page passes.
 *
 * In a frame there is no navigation at all: no sidebar, no app bar and no
 * tabs, so the page of the frame is the content and the person navigates in
 * the app that holds the frame.
 */
export default function Shell({
  section = "",
  query = "",
  children,
}: {
  section?: string;
  query?: string;
  children: ReactNode;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const frame = embedded();

  useEffect(() => {
    if (frame) {
      return;
    }
    let live = true;
    getJson<Session>("/session")
      .then((answer) => {
        if (live) {
          setSession(answer);
        }
      })
      .catch(() => {
        // The navigation is not the page: with no session the page still
        // shows its own body and its own error.
      });
    return () => {
      live = false;
    };
  }, [frame]);

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
  // The chat fills the screen: its own column scrolls, and the page does not.
  const fill = section === "chat";
  return (
    <div className={fill ? "j-shell has-sidebar fill" : "j-shell has-sidebar"}>
      <aside className="j-sidebar">
        <a className="j-brand" href="/">
          <img className="j-logo" src="/logo-64.png" alt="" />
          Joshua
        </a>
        <nav className="v-sections" aria-label="Sections">
          {sections.map((item) => (
            <NavItem
              key={item.key}
              path={item.path}
              migrated={isMigrated(item.key)}
              className="j-nav-item"
              mark={current(item.key, section)}
            >
              <Icon paths={iconOf(item.key)} />
              {item.label}
            </NavItem>
          ))}
        </nav>
        <LinksNav section={section} />
        <nav className="v-sections v-you" aria-label="You">
          <Link className="j-nav-item" to="/me" aria-current={current(ME_SECTION, section)}>
            <Icon paths={ME_ICON} />
            Your page
          </Link>
        </nav>
      </aside>
      <div className="j-main">
        <header className="j-appbar">
          <img className="j-logo" src="/logo-64.png" alt="" />
          <a className="j-appbar-title" href="/">
            <small>Joshua</small>
            <strong>{title(section)}</strong>
          </a>
          <span className="j-spacer"></span>
          <SearchField query={query} />
        </header>
        <nav className="j-tabs mobile-only" aria-label="Sections">
          {sections.map((item) => (
            <NavItem
              key={item.key}
              path={item.path}
              migrated={isMigrated(item.key)}
              className="j-tab"
              mark={current(item.key, section)}
            >
              {item.label}
            </NavItem>
          ))}
          <Link className="j-tab" to={LINKS_PATH} aria-current={current(LINKS_SECTION, section)}>
            Links
          </Link>
        </nav>
        <main className={contentClass(section)}>{children}</main>
      </div>
    </div>
  );
}

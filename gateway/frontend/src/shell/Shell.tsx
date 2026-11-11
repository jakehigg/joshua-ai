import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { getJson, type Person } from "../api/client";
import Icon from "./Icon";
import SearchField from "./SearchField";
import { ME_ICON, SECTIONS, linkIcon, section as sectionOf } from "./sections";

/** One section of the navigation, as `GET /api/v1/session` sends it. */
export interface SessionSection {
  key: string;
  label: string;
  path: string;
}

/** One link of the sidebar, as `GET /api/v1/session` sends it. */
export interface SessionLink {
  title: string;
  url: string;
  icon: string;
}

/** The answer of `GET /api/v1/session`. */
export interface Session {
  person: Person;
  anonymous: boolean;
  sections: SessionSection[];
  chat_url: string;
  links: SessionLink[];
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
  if (section === "links") {
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
 * The shell of every page: the sidebar on a wide screen, the app bar and the
 * tabs on a phone. `section` marks the current item. The search field is in
 * the app bar, so it shows on every screen; it goes to `/search`, a page of
 * this app. `query` is what the field holds, which the search page passes.
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

  useEffect(() => {
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
  }, []);

  const sections = session?.sections ?? [];
  const links = session?.links ?? [];
  return (
    <div className="j-shell has-sidebar">
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
        <div className="j-label">Links</div>
        <nav className="v-sections v-links" aria-label="Links">
          {links.map((link) => (
            <a
              className="j-nav-item"
              key={link.url}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon paths={linkIcon(link.icon)} />
              <span>{link.title}</span>
            </a>
          ))}
          <a
            className="j-nav-item sub v-links-edit"
            href="/links"
            aria-current={current("links", section)}
          >
            {links.length > 0 ? "Edit links" : "Add links"}
          </a>
        </nav>
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
          <a className="j-tab" href="/links" aria-current={current("links", section)}>
            Links
          </a>
        </nav>
        <main className={section === "usage" ? "j-content wide" : "j-content"}>{children}</main>
      </div>
    </div>
  );
}

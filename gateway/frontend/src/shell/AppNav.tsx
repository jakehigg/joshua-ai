// The app-wide navigation links: the sections of the session, the links
// group, and "Your page". The sidebar of the shell draws them, and on a phone
// in the chat the rail draws them too, under its workspaces, so the phone has
// one drawer for both (Rail.tsx). The shell puts `sections` and `section` in
// a context so the rail can read them without a second session call.

import { createContext, useContext, type ReactNode } from "react";
import { Link } from "react-router-dom";

import Icon from "./Icon";
import LinksNav from "./LinksNav";
import { ME_ICON, section as sectionOf } from "./sections";

export interface SessionSection {
  key: string;
  label: string;
  path: string;
}

/** The key of the item of "Your page". */
export const ME_SECTION = "me";

export interface NavState {
  sections: SessionSection[];
  /** The key of the current section. */
  section: string;
}

export const NavContext = createContext<NavState | null>(null);

export function useNav(): NavState | null {
  return useContext(NavContext);
}

export function current(key: string, section: string): "page" | undefined {
  return key === section ? "page" : undefined;
}

function isMigrated(key: string): boolean {
  return sectionOf(key)?.migrated ?? false;
}

function iconOf(key: string): string {
  return sectionOf(key)?.icon ?? "";
}

/**
 * One item of the navigation. A section this app renders is a router link; a
 * section the server still renders is a plain link, so the browser loads the
 * page.
 */
function NavItem({
  path,
  migrated,
  mark,
  onNavigate,
  children,
}: {
  path: string;
  migrated: boolean;
  mark: "page" | undefined;
  onNavigate?: () => void;
  children: ReactNode;
}) {
  if (migrated) {
    return (
      <Link className="j-nav-item" to={path} aria-current={mark} onClick={onNavigate}>
        {children}
      </Link>
    );
  }
  return (
    <a className="j-nav-item" href={path} aria-current={mark} onClick={onNavigate}>
      {children}
    </a>
  );
}

export default function AppNav({
  sections,
  section,
  onNavigate,
  omit = [],
}: NavState & {
  onNavigate?: () => void;
  /** Section keys not to draw: the rail of the chat has its own Chat entry. */
  omit?: string[];
}) {
  return (
    <>
      <nav className="v-sections" aria-label="Sections">
        {sections
          .filter((item) => !omit.includes(item.key))
          .map((item) => (
          <NavItem
            key={item.key}
            path={item.path}
            migrated={isMigrated(item.key)}
            mark={current(item.key, section)}
            onNavigate={onNavigate}
          >
            <Icon paths={iconOf(item.key)} />
            {item.label}
          </NavItem>
        ))}
      </nav>
      <LinksNav section={section} />
      <nav className="v-sections v-you" aria-label="You">
        <Link
          className="j-nav-item"
          to="/me"
          aria-current={current(ME_SECTION, section)}
          onClick={onNavigate}
        >
          <Icon paths={ME_ICON} />
          Your page
        </Link>
      </nav>
    </>
  );
}

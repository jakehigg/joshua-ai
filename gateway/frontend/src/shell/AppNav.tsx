// The app-wide navigation links: the sections of the session, the links
// group, "Profile", "Admin" for the owner, and "Reload". The sidebar draws
// them, as a column from 760px and as the drawer of the phone below it. Under
// the Chat item it draws the one list of threads (ChatList.tsx). The shell
// puts `sections` and `section` in a context, so a screen can read them
// without a second session call.

import { createContext, useContext, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { requestReload } from "../pwa";
import ChatList from "./ChatList";
import Icon from "./Icon";
import LinksNav from "./LinksNav";
import { ADMIN_ICON, ME_ICON, RELOAD_ICON, section as sectionOf } from "./sections";

export interface SessionSection {
  key: string;
  label: string;
  path: string;
}

/** The key of the item of "Profile". */
export const ME_SECTION = "me";

/** The key of the admin area, which only the owner sees in the navigation. */
export const ADMIN_NAV = "admin";

/** The key of the chat section, which carries the list of the threads. */
export const CHAT_SECTION = "chat";

export interface NavState {
  sections: SessionSection[];
  /** The key of the current section. */
  section: string;
  /** True for the owner, who gets "Admin" under "Profile". */
  owner?: boolean;
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
 * One item of the navigation. A section that is a route of this app is a
 * router link; any other section is a plain link, so the browser loads the
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

/**
 * The item that loads the page again.
 *
 * The installed app has no address bar and no reload button, and the chat
 * screen takes the pull of a finger for its own list, so a page that shows a
 * state it cannot clear has no other way out. The item is in this navigation,
 * which the sidebar draws on a wide screen and the drawer on a phone, so it
 * is one or two taps away on every page.
 *
 * A send that is going out holds the reload (`pwa.ts`): the item then says
 * that it waits, and the page loads again when the send is done.
 */
function ReloadItem() {
  const [waiting, setWaiting] = useState(false);
  return (
    <button
      type="button"
      className="j-nav-item"
      onClick={() => {
        if (!requestReload()) setWaiting(true);
      }}
    >
      <Icon paths={RELOAD_ICON} />
      {waiting ? "Reloading after the send" : "Reload"}
    </button>
  );
}

export default function AppNav({
  sections,
  section,
  owner = false,
  onNavigate,
}: NavState & {
  onNavigate?: () => void;
}) {
  return (
    <>
      <nav className="v-sections" aria-label="Sections">
        {sections.map((item) => (
          <div key={item.key} className="v-section">
            <NavItem
              path={item.path}
              migrated={isMigrated(item.key)}
              mark={current(item.key, section)}
              onNavigate={onNavigate}
            >
              <Icon paths={iconOf(item.key)} />
              {item.label}
            </NavItem>
            {item.key === CHAT_SECTION && (
              <ChatList chatPath={item.path} onNavigate={onNavigate} />
            )}
          </div>
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
          Profile
        </Link>
        {owner && (
          <Link
            className="j-nav-item"
            to="/admin"
            aria-current={current(ADMIN_NAV, section)}
            onClick={onNavigate}
          >
            <Icon paths={ADMIN_ICON} />
            Admin
          </Link>
        )}
      </nav>
      <div className="v-sections v-app">
        <ReloadItem />
      </div>
    </>
  );
}

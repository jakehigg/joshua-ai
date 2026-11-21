// The one list of threads, under the Chat item of the app navigation: the
// normal chat, the workspaces of the person with the newest activity first,
// a row that shows every one of them, and the button that makes a new one.
//
// The list is in the sidebar, which is a column from 760px and the drawer of
// the phone below it, so a person reaches every thread from every page and
// the chat screen itself holds no second list. A workspace row carries the
// two-person mark of a shared workspace, the charm of what is happening in it
// (WorkCharm.tsx) and the count of the rows this person has not read, in one
// column at the right edge; a long title truncates before them (viewer.css).
// Every link is `/chat/w/<id>`, the permanent address of a workspace.
//
// Which workspaces the collapsed list shows:
//
// - An active one and a paused one, the newest activity first, up to RECENT of
//   them. A paused workspace is still a thread a person reads and writes in,
//   so it stands in the list; the header of the workspace says it is paused.
// - The workspace the person is in, always, in its place by activity or at the
//   end of the five when it is older than all of them. The thread on screen is
//   never missing from the list.
// - An archived one only after "Show all", and marked as archived there, so a
//   closed workspace is reachable and never in the way.
//
// "Show all" is kept in localStorage (try/catch; without it the list starts
// collapsed), so a person who opened the whole list keeps it open after a
// reload.
//
// "New workspace" opens the dialog of the chat screen, which holds the form,
// so the button goes to the chat with `?new=1` (ChatPage.tsx).

import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { byActivity, linkOf, refLinkOf, type Workspace } from "../api/workspaces";
import { SharedMark } from "../chat/SharedMark";
import { WorkCharm } from "../chat/WorkCharm";
import { useWorkspaces } from "../chat/useWorkspaces";

/** How many workspaces the list shows before "Show all". */
export const RECENT = 5;

export const ALL_KEY = "joshua.chats.all";

function readAll(): boolean {
  try {
    return window.localStorage.getItem(ALL_KEY) === "1";
  } catch {
    return false;
  }
}

function saveAll(all: boolean): void {
  try {
    window.localStorage.setItem(ALL_KEY, all ? "1" : "0");
  } catch {
    // The choice holds for this screen only.
  }
}

export default function ChatList({
  chatPath,
  onNavigate,
}: {
  /** The address of the normal chat, `/chat/<person id>`. */
  chatPath: string;
  onNavigate?: () => void;
}) {
  const { pathname } = useLocation();
  const { list } = useWorkspaces();
  const [all, setAll] = useState(readAll);
  // The thread on screen: the permanent address of a workspace, or the
  // `<space>/<slug>` address a person may have opened it at.
  const here = (w: Workspace) => pathname === linkOf(w) || pathname === refLinkOf(w);
  const open = byActivity(list.filter((w) => w.status !== "archived"));
  const archived = byActivity(list.filter((w) => w.status === "archived"));
  // The collapsed list: the newest five, and the workspace the person is in
  // when it is not one of them, whatever its status.
  const recent = open.slice(0, RECENT);
  const current = list.find((w) => here(w) && !recent.includes(w));
  const shown = all ? [...open, ...archived] : current ? [...recent, current] : recent;
  const more = open.length > RECENT || archived.length > 0;
  const showAll = (next: boolean) => {
    setAll(next);
    saveAll(next);
  };
  return (
    <div className="v-chats">
      <Link
        className="j-nav-item sub"
        to={chatPath}
        aria-current={pathname === chatPath ? "page" : undefined}
        onClick={onNavigate}
      >
        <span className="v-chat-name">Chat</span>
      </Link>
      {shown.map((w) => (
        <Link
          key={w.id}
          className="j-nav-item sub"
          to={linkOf(w)}
          aria-current={here(w) ? "page" : undefined}
          onClick={onNavigate}
        >
          <span className="v-chat-name">{w.title}</span>
          <span className="v-chat-marks">
            {w.status === "archived" && <span className="v-chat-status">archived</span>}
            {w.shared && <SharedMark />}
            <WorkCharm charm={w.charm} />
            {(w.unread ?? 0) > 0 && (
              <span className="v-chat-unread" aria-label={`${w.unread} unread`}>
                {w.unread}
              </span>
            )}
          </span>
        </Link>
      ))}
      {more && (
        <button
          type="button"
          className="j-nav-item sub v-chats-more"
          aria-expanded={all}
          onClick={() => showAll(!all)}
        >
          {all ? "Show less" : "Show all"}
        </button>
      )}
      <Link
        className="j-nav-item sub v-chats-new"
        to={`${chatPath}?new=1`}
        onClick={onNavigate}
      >
        + New workspace
      </Link>
    </div>
  );
}

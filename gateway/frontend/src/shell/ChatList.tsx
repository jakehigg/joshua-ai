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
// "New workspace" opens the dialog of the chat screen, which holds the form,
// so the button goes to the chat with `?new=1` (ChatPage.tsx).

import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { byActivity, linkOf } from "../api/workspaces";
import { SharedMark } from "../chat/SharedMark";
import { WorkCharm } from "../chat/WorkCharm";
import { useWorkspaces } from "../chat/useWorkspaces";

/** How many workspaces the list shows before "Show all". */
export const RECENT = 5;

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
  const [all, setAll] = useState(false);
  const active = byActivity(list.filter((w) => w.status === "active"));
  const shown = all ? active : active.slice(0, RECENT);
  const mark = (path: string) => (pathname === path ? "page" : undefined);
  return (
    <div className="v-chats">
      <Link
        className="j-nav-item sub"
        to={chatPath}
        aria-current={mark(chatPath)}
        onClick={onNavigate}
      >
        <span className="v-chat-name">Chat</span>
      </Link>
      {shown.map((w) => (
        <Link
          key={w.id}
          className="j-nav-item sub"
          to={linkOf(w)}
          aria-current={mark(linkOf(w))}
          onClick={onNavigate}
        >
          <span className="v-chat-name">{w.title}</span>
          <span className="v-chat-marks">
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
      {active.length > RECENT && (
        <button
          type="button"
          className="j-nav-item sub v-chats-more"
          aria-expanded={all}
          onClick={() => setAll(!all)}
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

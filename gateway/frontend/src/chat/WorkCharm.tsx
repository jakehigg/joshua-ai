// The charm of a workspace: a small icon beside the name, in the rail and in
// the header of the workspace, next to the two-person mark of a shared one
// (SharedMark.tsx). It says that Joshua is working for this workspace, or
// that something of it waits for a person; with neither, there is no charm.
//
// Hover or tap gives the one line the viewer composed, such as "developer task
// running" or "assignment 31 has a question", and that line is also the
// accessible name, because the charm stands for no text on screen. It is
// read-only: nothing here acts.

import type { Charm } from "../api/workspaces";
import Icon from "../shell/Icon";
import { WAITING_ICON, WORKING_ICON } from "../shell/sections";

/** What a screen reader says first, before the line of why. */
const LABEL = { working: "Joshua is working", waiting: "Waiting for you" } as const;

export function WorkCharm({ charm }: { charm?: Charm | null }) {
  if (!charm || (charm.state !== "working" && charm.state !== "waiting")) return null;
  const label = LABEL[charm.state];
  const reason = charm.reason?.trim();
  return (
    <span
      className={`work-charm ${charm.state}`}
      role="img"
      aria-label={reason ? `${label}: ${reason}` : label}
      title={reason || label}
    >
      <Icon paths={charm.state === "working" ? WORKING_ICON : WAITING_ICON} />
    </span>
  );
}

/**
 * `/assignments`: the assignments of the person who signed in, one state at
 * a time.
 *
 * The state pills are the query of the address (`?state=`), so a bookmark
 * opens the same list. With no state the server picks: those in progress
 * when there is one, else the drafts when there is one, else the completed
 * ones. A row gives the title, the status, when it ran, how long it worked
 * and what it cost; every one of those strings comes from the server.
 *
 * With the manager not configured, or with a call that failed, the page says
 * so and no pill is selected.
 */

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import type { AssignmentList, AssignmentRow } from "../assignments/data";
import Shell from "../shell/Shell";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** The class of the badge of each status, the table of `viewer_assignments`. */
const STATUS_CLASS: Record<string, string> = {
  draft: "plain",
  running: "accent",
  waiting: "warn",
  paused_limits: "warn",
  done: "ok",
  failed: "bad",
  cancelled: "plain",
  budget_exceeded: "bad",
  time_exceeded: "bad",
};

/** What the page says with no manager, and what the viewer needs for one. */
export const NOT_CONFIGURED =
  "Assignments are not configured. The viewer needs ASSIGNMENTS_URL and JOSHUA_TOKEN_VIEWER.";
export const NO_ANSWER =
  "The assignments manager did not answer. The assignments are not available now.";

/** The status of an assignment, as a badge. */
export function StatusChip({ status, label }: { status: string; label: string }) {
  return <span className={`j-badge ${STATUS_CLASS[status] ?? "plain"}`}>{label}</span>;
}

/**
 * What a page says when the manager is not configured or did not answer, or
 * nothing when it answered.
 */
export function Unavailable({
  configured,
  available,
}: {
  configured: boolean;
  available: boolean;
}) {
  if (configured && available) {
    return null;
  }
  return <p className="j-empty">{configured ? NO_ANSWER : NOT_CONFIGURED}</p>;
}

/** The parts of the line under the title, those that are there. */
function sub(parts: string[]): string {
  return parts.filter((part) => part !== "").join(" · ");
}

/** One row: the title, what it is about, its marks, and its numbers. */
function Row({ item }: { item: AssignmentRow }) {
  return (
    <li className="j-list-row a-row">
      <div className="j-list-main">
        <Link className="j-list-title wrap" to={item.url}>
          {item.title}
        </Link>
        {item.summary_line !== "" && <span className="a-row-line">{item.summary_line}</span>}
        {item.start_phrase !== "" && (
          <span className="a-row-line j-muted">{item.start_phrase}</span>
        )}
        <span className="j-list-sub">{sub([item.when, item.duration, item.cost])}</span>
      </div>
      <div className="a-chips">
        {item.asks && (
          <span className="j-badge warn plain" title="The assignment asks a question">
            question
          </span>
        )}
        <StatusChip status={item.status} label={item.status_label} />
      </div>
    </li>
  );
}

export default function Assignments() {
  const [params] = useSearchParams();
  const state = params.get("state") ?? "";
  const [list, setList] = useState<AssignmentList | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setList(null);
    setError("");
    getJson<AssignmentList>(
      state === "" ? "/assignments" : `/assignments?state=${encodeURIComponent(state)}`,
    )
      .then((answer) => {
        if (live) {
          setList(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the assignments did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [state]);

  useEffect(() => {
    document.title = "assignments";
  }, []);

  if (error !== "") {
    return (
      <Shell section="assignments">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (list === null) {
    return (
      <Shell section="assignments">
        <Spinner label="Loading the assignments" />
      </Shell>
    );
  }
  const ready = list.configured && list.available;
  return (
    <Shell section="assignments">
      <Page title="assignments">
        <div className="a-bar">
          <div className="j-chips" role="group" aria-label="State">
            {list.states.map((item) =>
              item.key === list.state ? (
                <span className="j-chip on" aria-current="true" key={item.key}>
                  {item.label}
                </span>
              ) : (
                <Link className="j-chip" to={`/assignments?state=${item.key}`} key={item.key}>
                  {item.label}
                </Link>
              ),
            )}
          </div>
          <a className="a-saved-link" href="/assignments/saved">
            Saved
          </a>
        </div>
        <Unavailable configured={list.configured} available={list.available} />
        {ready &&
          (list.items.length === 0 ? (
            <Empty text={list.empty} />
          ) : (
            <ul className="j-list a-list">
              {list.items.map((item) => (
                <Row item={item} key={item.id} />
              ))}
            </ul>
          ))}
      </Page>
    </Shell>
  );
}

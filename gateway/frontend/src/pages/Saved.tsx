/**
 * `/assignments/saved`: the saved assignments of the person who signed in.
 *
 * A saved assignment is a definition Joshua keeps by name, so a person asks
 * for it again. One row gives its name, what it is about, how often it ran,
 * when it ran last and how that run ended; every one of those strings comes
 * from the server.
 *
 * With the manager not configured, or with a call that failed, the page says
 * so and shows no row.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import type { SavedList, SavedRow } from "../assignments/data";
import Shell from "../shell/Shell";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";
import { CommentsBadge } from "./Documents";
import { StatusChip, Unavailable } from "./Assignments";

/** `3 runs`, `1 run`. */
function count(n: number, noun: string): string {
  return `${String(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/** One row: the name, the schedule, the runs, and the last one of them. */
function Row({ item }: { item: SavedRow }) {
  const last = item.last_run;
  return (
    <li className="j-list-row a-row">
      <div className="j-list-main">
        <Link className="j-list-title wrap" to={item.url}>
          {item.name}
        </Link>
        {item.title !== "" && <span className="j-list-sub">{item.title}</span>}
        <span className="j-list-sub">
          {count(item.run_count, "run")}
          {item.schedule !== "" && ` · ${item.schedule}`}
          {last !== null && (
            <>
              {" · "}
              <Link to={last.url}>last run</Link>
              {last.when !== "" && ` ${last.when}`}
            </>
          )}
        </span>
      </div>
      <div className="a-chips">
        <CommentsBadge open={item.open_comments} />
        {last !== null && <StatusChip status={last.status} label={last.status_label} />}
      </div>
    </li>
  );
}

export default function Saved() {
  const [list, setList] = useState<SavedList | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    getJson<SavedList>("/assignments/saved")
      .then((answer) => {
        if (live) {
          setList(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the saved assignments did not load");
        }
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    document.title = "saved assignments";
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
        <Spinner label="Loading the saved assignments" />
      </Shell>
    );
  }
  const ready = list.configured && list.available;
  return (
    <Shell section="assignments">
      <nav className="j-crumbs">
        <Link to="/assignments">assignments</Link>
      </nav>
      <Page title="saved assignments">
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

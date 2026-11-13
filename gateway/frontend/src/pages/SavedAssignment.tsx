/**
 * `/assignments/saved/:id`: one saved assignment of the person who signed in.
 *
 * The page is the definition Joshua keeps by name, open, and the runs it
 * made, the newest first, each one linking to its assignment. A saved
 * assignment takes a comment on its definition only, which `targets` says, so
 * there is one gutter on the page.
 *
 * A row of another person is 404, and the page says what the viewer said.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Definition from "../assignments/Definition";
import type { SavedData, SavedRun } from "../assignments/data";
import type { DocComment } from "../comments/CommentCard";
import Shell, { type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";
import { StatusChip, Unavailable } from "./Assignments";

/** `3 runs`, `1 run`. */
function count(n: number, noun: string): string {
  return `${String(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/** The parts of the line under the title, those that are there. */
function sub(parts: string[]): string {
  return parts.filter((part) => part !== "").join(" · ");
}

/** One run of the saved assignment, by its number of that saved one. */
function Run({ run }: { run: SavedRun }) {
  return (
    <li className="j-list-row a-row">
      <div className="j-list-main">
        <Link className="j-list-title wrap" to={run.url}>
          {`Run ${String(run.number)}: ${run.title}`}
        </Link>
        {run.summary_line !== "" && <span className="a-row-line">{run.summary_line}</span>}
        <span className="j-list-sub">{sub([run.when, run.duration, run.cost])}</span>
      </div>
      <div className="a-chips">
        <StatusChip status={run.status} label={run.status_label} />
      </div>
    </li>
  );
}

export default function SavedAssignment() {
  const { id = "" } = useParams();
  const path = `/assignments/saved/${encodeURIComponent(id)}`;
  const [data, setData] = useState<SavedData | null>(null);
  const [comments, setComments] = useState<DocComment[]>([]);
  const [person, setPerson] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [reads, setReads] = useState(0);

  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);

  useEffect(() => {
    let live = true;
    getJson<SavedData>(path)
      .then((answer) => {
        if (live) {
          setData(answer);
          setComments(answer.comments);
          setError("");
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the saved assignment did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [path, reads]);

  useEffect(() => {
    let live = true;
    getJson<Session>("/session")
      .then((answer) => {
        if (live) {
          setPerson(answer.person.id);
        }
      })
      .catch(() => {
        // Only Delete needs to know who signed in.
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    document.title = data?.item == null ? "saved assignment" : data.item.name;
  }, [data]);

  const reload = useCallback(() => {
    setReads((n) => n + 1);
  }, []);

  const crumbs = (
    <nav className="j-crumbs">
      <Link to="/assignments">assignments</Link> <span aria-hidden="true">/</span>{" "}
      <Link to="/assignments/saved">saved</Link>
    </nav>
  );

  if (error !== "") {
    return (
      <Shell section="assignments">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (data === null) {
    return (
      <Shell section="assignments">
        <Spinner label="Loading the saved assignment" />
      </Shell>
    );
  }
  const item = data.item;
  if (item === null || data.definition === null) {
    return (
      <Shell section="assignments">
        {crumbs}
        <h1 className="j-doc-title">saved assignment</h1>
        <Unavailable configured={data.configured} available={data.available} />
      </Shell>
    );
  }
  return (
    <Shell section="assignments">
      {crumbs}
      <h1 className="j-doc-title">{item.name}</h1>
      {item.title !== "" && <p className="a-lead">{item.title}</p>}
      <p className="j-meta">
        {sub([
          count(item.run_count, "run"),
          item.schedule,
          item.updated === "" ? "" : `updated ${item.updated}`,
        ])}
      </p>
      <Definition
        url={path}
        definition={data.definition}
        comments={comments}
        csrf={data.targets.includes("definition") ? data.csrf : null}
        person={person}
        onComments={setComments}
        reload={reload}
        open
      />
      <section className="a-section" id="runs">
        <div className="a-head">
          <h2>Runs</h2>
        </div>
        {data.runs.length === 0 ? (
          <p className="j-faint">No run yet.</p>
        ) : (
          <ul className="j-list a-list">
            {data.runs.map((run) => (
              <Run run={run} key={run.id} />
            ))}
          </ul>
        )}
      </section>
    </Shell>
  );
}

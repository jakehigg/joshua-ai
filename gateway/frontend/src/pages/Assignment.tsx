/**
 * `/assignments/:id`: one assignment of the person who signed in.
 *
 * The page is the assignment, top to bottom: the hero with its status and
 * its numbers, what it waits for, the definition as a document, the plan
 * with its progress, the outputs and the checkpoints, and the report. A
 * draft shows its definition open and nothing of a run.
 *
 * A comment on a line of the definition, the plan or the report goes to the
 * manager, which stores it; the viewer keeps nothing. The manager sends no
 * comment back in the answer of a write, so the page reads itself again.
 *
 * While the assignment is in progress the server sends `refresh_seconds` and
 * the page reads itself again that often. The answer of a finished
 * assignment sends none, so the polling stops.
 *
 * A row of another person is 404, and the page says what the viewer said.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Blocks, { spans } from "../blocks/Blocks";
import Definition from "../assignments/Definition";
import Plan from "../assignments/Plan";
import Progress, { Notice } from "../assignments/Progress";
import { NO_COMMENTS, type AssignmentData, type AssignmentItem, type CommentSet } from "../assignments/data";
import type { DocComment } from "../comments/CommentCard";
import { Orphans, Resolved, useGutter } from "../comments/Gutter";
import Shell, { type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Fold from "../ui/Fold";
import Spinner from "../ui/Spinner";
import { CommentsBadge } from "./Documents";
import { StatusChip, Unavailable } from "./Assignments";

/** One number of the hero, with its label. */
function Tile({ value, label, title, text }: { value: string; label: string; title?: string; text?: boolean }) {
  if (value === "") {
    return null;
  }
  return (
    <div className={text === true ? "j-stat a-stat-text" : "j-stat"} title={title}>
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}

/** The head of the page: the marks, the title, and the numbers. */
function Hero({ item, open }: { item: AssignmentItem; open: number }) {
  return (
    <header className="a-hero">
      <div className="a-chips">
        <StatusChip status={item.status} label={item.status_label} />
        {item.saved_ref !== null && (
          <a className="j-chip a-saved" href={`/assignments/saved/${String(item.saved_ref.id)}`}>
            {`saved: ${item.saved_ref.name}`}
          </a>
        )}
        {item.run_number !== null && (
          <a className="j-chip a-run" href="#lineage">
            {`run ${String(item.run_number.run)} of assignment ${String(item.run_number.root)}`}
          </a>
        )}
        <CommentsBadge open={open} />
        {item.when !== "" && <span className="j-meta">{item.when}</span>}
      </div>
      <h1 className="j-doc-title">{item.title}</h1>
      {item.start_phrase !== "" && <p className="a-lede a-start-line">{item.start_phrase}</p>}
      {item.draft_line !== "" && <p className="a-lede a-draft-line">{item.draft_line}</p>}
      <div className="j-stats a-stats" role="group" aria-label="Numbers">
        <Tile value={item.cost} label="cost" title={item.cost_title} />
        <Tile value={item.duration} label="working time" />
        <Tile value={item.turns === null ? "" : String(item.turns)} label="turns" />
        <Tile value={item.model} label="model" text />
      </div>
    </header>
  );
}

/** Each run of the lineage, the oldest first. */
function Lineage({ item }: { item: AssignmentItem }) {
  if (item.run_number === null) {
    return null;
  }
  return (
    <div id="lineage">
      <Fold title="Runs of this assignment" hint={`${String(item.lineage.length)} runs`}>
        <ol className="a-lineage">
          {item.lineage.map((run, index) => (
            <li className="a-lineage-run" key={run.id}>
              {run.current ? (
                <b aria-current="page">{`Run ${String(index + 1)}: assignment ${String(run.id)} (this run)`}</b>
              ) : (
                <Link to={run.url}>{`Run ${String(index + 1)}: assignment ${String(run.id)}`}</Link>
              )}
              {run.title !== "" && <span className="j-list-sub">{run.title}</span>}
              <span className="a-chips">
                <StatusChip status={run.status} label={run.status_label} />
                {run.when !== "" && <span className="j-faint">{run.when}</span>}
              </span>
            </li>
          ))}
        </ol>
      </Fold>
    </div>
  );
}

export default function Assignment() {
  const { id = "" } = useParams();
  const path = `/assignments/${encodeURIComponent(id)}`;
  const [data, setData] = useState<AssignmentData | null>(null);
  const [comments, setComments] = useState<CommentSet>(NO_COMMENTS);
  const [person, setPerson] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [reads, setReads] = useState(0);

  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);

  useEffect(() => {
    let live = true;
    getJson<AssignmentData>(path)
      .then((answer) => {
        if (live) {
          setData(answer);
          setComments(answer.comments);
          setError("");
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the assignment did not load");
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
    document.title = data?.item === null || data === null ? "assignment" : data.item.title;
  }, [data]);

  const refresh = data?.refresh_seconds ?? null;
  useEffect(() => {
    if (refresh === null || refresh <= 0) {
      return undefined;
    }
    const timer = setInterval(() => {
      setReads((n) => n + 1);
    }, refresh * 1000);
    return () => {
      clearInterval(timer);
    };
  }, [refresh, path]);

  const reload = useCallback(() => {
    setReads((n) => n + 1);
  }, []);

  /** Keep the comments of one text, when a route ever sends them back. */
  function took(target: keyof CommentSet): (next: DocComment[]) => void {
    return (next) => {
      setComments((old) => ({ ...old, [target]: next }));
    };
  }

  const report = data?.report ?? null;
  const reportUnits = useMemo(() => spans(report?.blocks ?? []), [report]);
  const reportGutter = useGutter({
    url: path,
    lines: report?.lines ?? [],
    comments: comments.report,
    csrf: data?.csrf ?? null,
    person,
    units: reportUnits,
    onComments: took("report"),
    prefix: "R",
    fields: { target: "report" },
    reload,
  });

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
        <Spinner label="Loading the assignment" />
      </Shell>
    );
  }
  const item = data.item;
  if (item === null || data.definition === null || data.plan === null) {
    return (
      <Shell section="assignments">
        <nav className="j-crumbs">
          <Link to="/assignments">assignments</Link>
        </nav>
        <h1 className="j-doc-title">assignment</h1>
        <Unavailable configured={data.configured} available={data.available} />
      </Shell>
    );
  }
  const draft = item.state === "draft";
  return (
    <Shell section="assignments">
      <nav className="j-crumbs">
        <Link to="/assignments">assignments</Link>
      </nav>
      <Hero item={item} open={item.open_comments} />
      <Notice notice={item.notice} />
      <Definition
        url={path}
        definition={data.definition}
        comments={comments.definition}
        csrf={data.csrf}
        person={person}
        onComments={took("definition")}
        reload={reload}
        open={draft}
      />
      {!draft && (
        <>
          <Plan
            url={path}
            plan={data.plan}
            comments={comments.plan}
            csrf={data.csrf}
            person={person}
            onComments={took("plan")}
            reload={reload}
          />
          <Progress
            outputs={item.outputs}
            checkpoints={item.checkpoints}
            done={item.state === "completed"}
          />
          <section className="a-section" id="report">
            <div className="a-head">
              <h2>Report</h2>
              {report !== null && report.updated !== "" && (
                <span className="j-meta">{`updated ${report.updated}`}</span>
              )}
            </div>
            {report === null ? (
              <p className="j-faint">No report yet.</p>
            ) : (
              <>
                <Blocks blocks={report.blocks} gutter={reportGutter} />
                {reportGutter.error !== "" && <ErrorLine text={reportGutter.error} />}
                <Orphans gutter={reportGutter} />
                <Resolved gutter={reportGutter} />
              </>
            )}
          </section>
          <Lineage item={item} />
        </>
      )}
      {item.error !== "" && (
        <section className="a-section">
          <div className="a-head">
            <h2>Error</h2>
          </div>
          <pre className="a-error">{item.error}</pre>
        </section>
      )}
    </Shell>
  );
}

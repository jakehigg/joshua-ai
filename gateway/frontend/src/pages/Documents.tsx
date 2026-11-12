/**
 * `/documents`: the documents of the member who signed in and the shared
 * ones, one status at a time.
 *
 * The status tabs (Active, Drafts, Final, Archived) and the kind chips are
 * the query of the address (`?status=&kind=`), so a bookmark opens the same
 * list. A row gives the title, the kind, the status, the time of the last
 * change, the owner, the open comments, and the summary. There is no table.
 * No other member's document is in the answer, and a guest gets 404.
 */

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import { When } from "../comments/CommentCard";
import Shell from "../shell/Shell";
import Chip from "../ui/Chip";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** One document of the list, as `GET /api/v1/documents` sends it. */
export interface DocumentRow {
  path: string;
  url: string;
  title: string;
  kind: string;
  status: string;
  owner: string;
  owner_label: string;
  summary: string;
  updated: string | null;
  open_comments: number;
}

/** The answer of `GET /api/v1/documents`. */
export interface DocumentList {
  status: string;
  kind: string | null;
  counts: Record<string, number>;
  kinds: string[];
  documents: DocumentRow[];
}

/** The tabs of the list, in order: the status, its label, and its empty text. */
export const PILLS: readonly { key: string; label: string; empty: string }[] = [
  { key: "active", label: "Active", empty: "No active document." },
  { key: "draft", label: "Drafts", empty: "No draft." },
  { key: "final", label: "Final", empty: "No final document." },
  { key: "archived", label: "Archived", empty: "No archived document." },
];

/** What a list with no document at all says. */
export const EMPTY_TEXT =
  "No documents yet. Joshua keeps a plan, a report, or a spec here when you ask for one.";

const STATUS_CLASS: Record<string, string> = {
  draft: "plain",
  active: "accent",
  final: "ok",
  archived: "plain",
};

/** The status of a document, as a badge. */
export function StatusChip({ status }: { status: string }) {
  return <span className={`j-badge ${STATUS_CLASS[status] ?? "plain"}`}>{status}</span>;
}

/** The kind of a document, as a tag. */
export function KindChip({ kind }: { kind: string }) {
  return <span className="a-tag d-kind">{kind}</span>;
}

/** How many comments wait for Joshua, or nothing when none do. */
export function CommentsBadge({ open }: { open: number }) {
  if (open <= 0) {
    return null;
  }
  return (
    <span className="j-badge accent a-comments">
      {`${open} comment${open === 1 ? "" : "s"} for Joshua`}
    </span>
  );
}

/** What an empty list of one status says. */
export function emptyText(list: DocumentList): string {
  const total = Object.values(list.counts).reduce((sum, n) => sum + n, 0);
  if (total === 0 && list.kind === null) {
    return EMPTY_TEXT;
  }
  const pill = PILLS.find((item) => item.key === list.status);
  const text = pill?.empty ?? EMPTY_TEXT;
  return list.kind === null ? text : `${text.slice(0, -1)} of the kind ${list.kind}.`;
}

/** One row: the title, the summary, and the marks of the document. */
function Row({ doc }: { doc: DocumentRow }) {
  return (
    <li className="j-list-row a-row d-row">
      <div className="j-list-main">
        <Link className="j-list-title wrap" to={doc.url}>
          {doc.title}
        </Link>
        {doc.summary !== "" && <span className="a-row-line">{doc.summary}</span>}
        <span className="j-list-sub">
          <KindChip kind={doc.kind} /> · {doc.owner_label}
          {doc.updated !== null && (
            <>
              {" · updated "}
              <When at={doc.updated} />
            </>
          )}
        </span>
      </div>
      <div className="a-chips">
        <CommentsBadge open={doc.open_comments} />
        <StatusChip status={doc.status} />
      </div>
    </li>
  );
}

export default function Documents() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const kind = params.get("kind") ?? "";
  const [list, setList] = useState<DocumentList | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setList(null);
    setError("");
    const query = new URLSearchParams();
    if (status !== "") {
      query.set("status", status);
    }
    if (kind !== "") {
      query.set("kind", kind);
    }
    const suffix = query.toString();
    getJson<DocumentList>(suffix === "" ? "/documents" : `/documents?${suffix}`)
      .then((answer) => {
        if (live) {
          setList(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the documents did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [status, kind]);

  useEffect(() => {
    document.title = "documents";
  }, []);

  /** Write one filter into the address; an empty value drops it. */
  function pick(name: string, value: string): void {
    const next = new URLSearchParams(params);
    if (value === "") {
      next.delete(name);
    } else {
      next.set(name, value);
    }
    setParams(next);
  }

  if (error !== "") {
    return (
      <Shell section="documents">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (list === null) {
    return (
      <Shell section="documents">
        <Spinner label="Loading the documents" />
      </Shell>
    );
  }
  return (
    <Shell section="documents">
      <Page title="documents">
        <div className="a-bar d-bar">
          <div className="j-chips" role="group" aria-label="Status">
            {PILLS.map((item) => (
              <Chip
                key={item.key}
                on={item.key === list.status}
                onClick={() => pick("status", item.key)}
              >
                {item.label}
              </Chip>
            ))}
          </div>
        </div>
        <div className="j-chips d-kinds" role="group" aria-label="Kind">
          <Chip on={list.kind === null} onClick={() => pick("kind", "")}>
            all kinds
          </Chip>
          {list.kinds.map((item) => (
            <Chip key={item} on={item === list.kind} onClick={() => pick("kind", item)}>
              {item}
            </Chip>
          ))}
        </div>
        {list.documents.length === 0 ? (
          <Empty text={emptyText(list)} />
        ) : (
          <ul className="j-list a-list d-list">
            {list.documents.map((doc) => (
              <Row key={doc.path} doc={doc} />
            ))}
          </ul>
        )}
      </Page>
    </Shell>
  );
}

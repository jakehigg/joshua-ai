/**
 * The comment gutter: the "+" of a unit, the form of a new comment, the
 * cards, and the comments that no unit took.
 *
 * A comment names one line of a text (`line_no`, from 1, and `line_text`,
 * the line as it was). Each part of the page that shows some lines is a
 * *unit*: it carries its first line and its last line, it has the "+" of its
 * first line, and it shows the open comments on any of its lines. These are
 * the rules of `viewer_comments`:
 *
 * - a comment shows under the first unit that holds its line, and the units
 *   are taken in the order of the page;
 * - a comment on a line of a unit that is not its first line quotes the line;
 * - `stale` is true when the stored `line_text` is no longer that line, and
 *   the card says so;
 * - the comments that no unit took show under one label at the end;
 * - the resolved comments are folded;
 * - with no `csrf` the gutter is read-only: it shows the cards and no "+".
 *
 * The form posts JSON to `<url>/comments`: a document
 * (`/api/v1/documents/<owner>/<slug>`) or an assignment
 * (`/api/v1/assignments/<id>`). `fields` are the fields the body carries
 * beside the comment, which is the `target` of an assignment: the
 * definition, the plan, or the report. When the answer carries the comments,
 * as a document does, the page needs no reload; when it does not, as the
 * assignments manager does not, `reload` reads the page again.
 *
 * One form is open at a time: the "+" of a unit and the "+" of the same line
 * in the Source fold are two forms, so a key names the one that is open.
 * `prefix` starts the id of each unit, so two texts on one page have no id
 * in common (`viewer_comments.Gutter`).
 */

import { useMemo, useState, type ReactNode } from "react";

import { ApiError, postJson } from "../api/client";
import ErrorLine from "../ui/ErrorLine";
import CommentCard, { type DocComment } from "./CommentCard";

/** The longest comment, in characters: `viewer_comments.MAX_COMMENT`. */
export const MAX_COMMENT = 2000;

/** The lines one unit of the page holds, both from 1. Line 0 is the head. */
export interface Span {
  first: number;
  last: number;
}

/** The answer of a write of a comment. */
interface CommentsAnswer {
  ok: boolean;
  /** The comments as the text now holds them, when the route sends them. */
  comments?: DocComment[];
}

/** The gutter of one text, as the parts of the page read it. */
export interface Gutter {
  /** The lines of the body, which a comment counts from 1. */
  lines: string[];
  /** The start of the id of each unit and each anchor: `L`, `P` or `R`. */
  prefix: string;
  /** The token of a write, or null when the gutter is read-only. */
  csrf: string | null;
  /** The person who signed in: only their own open comment has a Delete. */
  person: string;
  /** The open comments of each unit, by the first line of the unit. */
  cards: Map<number, DocComment[]>;
  /** The open comments that no unit took. */
  orphans: DocComment[];
  /** The resolved comments, which the fold shows. */
  resolved: DocComment[];
  /** The lines that hold an open comment, which the Source fold marks. */
  marked: Set<number>;
  /** How many comments are open. */
  open: number;
  /** The key of the "+" whose form is open, or null. */
  openForm: string | null;
  setOpenForm: (key: string | null) => void;
  /** What the viewer said about the last write, or "". */
  error: string;
  busy: boolean;
  add: (lineNo: number, lineText: string, body: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

/** The line a comment is on, as a label: `line 12`, or `the document`. */
function lineName(lineNo: number): string {
  return lineNo === 0 ? "the document" : `line ${lineNo}`;
}

/** The key of one "+": its line, and what it is on, which the place names. */
function formKey(line: number, what: string): string {
  return `${line}:${what}`;
}

/**
 * The gutter of one text.
 *
 * `units` are the spans of the page, in the order the page shows them, so a
 * comment lands on the first one that holds its line. `onComments` takes the
 * comments of an answer, so the page keeps one copy of them; a route that
 * sends no comments calls `reload` instead.
 */
export function useGutter({
  url,
  lines,
  comments,
  csrf,
  person,
  units,
  onComments,
  prefix = "L",
  fields = {},
  reload,
}: {
  url: string;
  lines: string[];
  comments: DocComment[];
  csrf: string | null;
  person: string;
  units: Span[];
  onComments: (next: DocComment[]) => void;
  prefix?: string;
  fields?: Record<string, unknown>;
  reload?: () => void;
}): Gutter {
  const [openForm, setOpenForm] = useState<string | null>(null);
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const placed = useMemo(() => {
    const open = comments.filter((item) => item.resolved_at === null);
    const cards = new Map<number, DocComment[]>();
    const taken = new Set<DocComment>();
    for (const unit of units) {
      const mine = open.filter(
        (item) =>
          !taken.has(item) && item.line_no >= unit.first && item.line_no <= unit.last,
      );
      for (const item of mine) {
        taken.add(item);
      }
      if (mine.length > 0) {
        cards.set(unit.first, [...(cards.get(unit.first) ?? []), ...mine]);
      }
    }
    return {
      cards,
      orphans: open.filter((item) => !taken.has(item)),
      resolved: comments.filter((item) => item.resolved_at !== null),
      marked: new Set(open.map((item) => item.line_no)),
      open: open.length,
    };
  }, [comments, units]);

  async function write(path: string, body: unknown, said: string): Promise<CommentsAnswer | null> {
    setBusy(true);
    setError("");
    try {
      return await postJson<CommentsAnswer>(path, body);
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : said);
      return null;
    } finally {
      setBusy(false);
    }
  }

  /** The comments of an answer, or a reload when the answer sends none. */
  function took(answer: CommentsAnswer): void {
    if (answer.comments === undefined) {
      reload?.();
    } else {
      onComments(answer.comments);
    }
  }

  return {
    ...placed,
    lines,
    prefix,
    csrf,
    person,
    openForm,
    setOpenForm,
    error,
    busy,
    async add(lineNo: number, lineText: string, body: string): Promise<void> {
      const answer = await write(
        `${url}/comments`,
        { ...fields, csrf, line_no: lineNo, line_text: lineText, body },
        "the comment was not stored",
      );
      if (answer !== null) {
        took(answer);
        setOpenForm(null);
      }
    },
    async remove(id: string): Promise<void> {
      const answer = await write(
        `${url}/comments/${encodeURIComponent(id)}/delete`,
        { ...fields, csrf },
        "the comment was not removed",
      );
      if (answer !== null) {
        took(answer);
      }
    },
  };
}

/**
 * The "+" of one line, and the form it opens.
 *
 * `what` names what the comment is on, as the page sees it ("the paragraph at
 * line 3"); with none, the label names the line. A read-only gutter has no
 * token, so it draws nothing.
 */
export function Plus({
  gutter,
  line,
  text,
  what = "",
}: {
  gutter: Gutter;
  line: number;
  text: string;
  what?: string;
}) {
  const [body, setBody] = useState("");
  if (gutter.csrf === null) {
    return null;
  }
  const label = `Comment on ${what === "" ? lineName(line) : what}`;
  const key = formKey(line, what);
  const open = gutter.openForm === key;
  if (!open) {
    return (
      <button
        className="v-plus"
        type="button"
        title={label}
        aria-label={label}
        onClick={() => {
          gutter.setOpenForm(key);
        }}
      >
        +
      </button>
    );
  }
  return (
    <form
      className="a-comment-form"
      onSubmit={(event) => {
        event.preventDefault();
        void gutter.add(line, text, body).then(() => {
          setBody("");
        });
      }}
    >
      <div className="j-field">
        <textarea
          rows={3}
          maxLength={MAX_COMMENT}
          required
          aria-label={`Your ${label[0]?.toLowerCase() ?? ""}${label.slice(1)}`}
          placeholder="Tell Joshua what to change"
          value={body}
          onChange={(event) => {
            setBody(event.target.value);
          }}
        />
      </div>
      {gutter.error !== "" && <ErrorLine text={gutter.error} />}
      <div className="v-plus-actions">
        <button className="j-btn sm" type="submit" disabled={gutter.busy}>
          Comment
        </button>
        <button
          className="j-btn sm"
          type="button"
          onClick={() => {
            gutter.setOpenForm(null);
          }}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The cards of one unit: the open comments the gutter placed on it. */
export function Cards({ gutter, unit }: { gutter: Gutter; unit: Span }) {
  const mine = gutter.cards.get(unit.first) ?? [];
  return (
    <>
      {mine.map((comment) => (
        <CommentCard
          key={comment.id}
          comment={comment}
          quote={comment.line_no !== unit.first}
          canDelete={gutter.csrf !== null && comment.author === gutter.person}
          onDelete={(id) => {
            void gutter.remove(id);
          }}
        />
      ))}
    </>
  );
}

/**
 * One unit of the page: an anchor for each of its lines after the first, the
 * body, the "+" of its first line, and the cards of its comments.
 */
export function Unit({
  gutter,
  unit,
  what,
  className = "",
  children,
}: {
  gutter: Gutter;
  unit: Span;
  what?: string;
  className?: string;
  children: ReactNode;
}) {
  const cards = gutter.cards.get(unit.first) ?? [];
  const classes = ["a-unit", className, cards.length > 0 ? "has-comments" : ""];
  const anchors: ReactNode[] = [];
  for (let line = unit.first + 1; line <= unit.last; line += 1) {
    anchors.push(<span className="a-anchor" key={line} id={`${gutter.prefix}${line}`} />);
  }
  return (
    <div
      className={classes.filter((part) => part !== "").join(" ")}
      id={`${gutter.prefix}${unit.first}`}
    >
      {anchors}
      <div className="a-unit-body">{children}</div>
      <Plus
        gutter={gutter}
        line={unit.first}
        text={gutter.lines[unit.first - 1] ?? ""}
        what={what}
      />
      <Cards gutter={gutter} unit={unit} />
    </div>
  );
}

/** The open comments that no unit took, under one label. */
export function Orphans({ gutter }: { gutter: Gutter }) {
  if (gutter.orphans.length === 0) {
    return null;
  }
  return (
    <div className="a-orphans">
      <p className="j-label">On lines that are gone</p>
      {gutter.orphans.map((comment) => (
        <CommentCard
          key={comment.id}
          comment={comment}
          canDelete={gutter.csrf !== null && comment.author === gutter.person}
          onDelete={(id) => {
            void gutter.remove(id);
          }}
        />
      ))}
    </div>
  );
}

/** The resolved comments, folded, each with the resolution of Joshua. */
export function Resolved({ gutter }: { gutter: Gutter }) {
  if (gutter.resolved.length === 0) {
    return null;
  }
  return (
    <details className="a-resolved">
      <summary>{`${gutter.resolved.length} resolved`}</summary>
      <ul className="a-resolved-list">
        {gutter.resolved.map((comment) => (
          <li key={comment.id}>
            <CommentCard comment={comment} />
          </li>
        ))}
      </ul>
    </details>
  );
}

/**
 * The text as it is stored: one row a line with its number and its "+".
 *
 * The `preamble` lines (the frontmatter of a document) come first, with no
 * number and no "+", because a comment counts the lines of the body only.
 */
export function SourceLines({
  gutter,
  preamble = [],
}: {
  gutter: Gutter;
  preamble?: string[];
}) {
  return (
    <ol className="a-lines">
      {preamble.map((line, index) => (
        <li className="a-line a-pre" key={`pre-${index}`}>
          <span className="a-ln" />
          <code className="a-code j-faint">{line}</code>
        </li>
      ))}
      {gutter.lines.map((line, index) => {
        const no = index + 1;
        return (
          <li
            className={gutter.marked.has(no) ? "a-line has-comments" : "a-line"}
            id={`S${no}`}
            key={no}
          >
            <a className="a-ln" href={`#S${no}`}>
              {no}
            </a>
            <code className="a-code">{line}</code>
            <Plus gutter={gutter} line={no} text={line} />
          </li>
        );
      })}
    </ol>
  );
}

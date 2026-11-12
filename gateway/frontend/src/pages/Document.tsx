/**
 * `/documents/:owner/:slug`: one document of the member who signed in, or a
 * shared one.
 *
 * The head gives the title, the marks of the document, the properties of its
 * frontmatter, and the "+" of line 0, the document as a whole. A Markdown
 * document shows as blocks, each one a unit of the comment gutter. The
 * `Source` fold gives the frontmatter with no "+" and each line of the body
 * with its own "+", and the `Versions` fold lists the commits of the file;
 * `?rev=<sha>` opens an older version, which is read-only.
 *
 * Embed mode (`?embed=1`, or any page in a frame, which is how the chat panel
 * loads it; `?rev=` works with it): only the body. There is no app shell, no
 * breadcrumb and no head card. The comment gutter works as on the full page.
 * The head, the Source fold and the Versions fold are in one closed `Details`
 * fold at the bottom, so nothing is lost. A body that is one code block with
 * a little prose around it (`codeFirst`) shows the code first, full width,
 * with its copy button, and the prose in a closed `Notes` fold below it. This
 * is a way to show a document; the stored document does not change. Code is
 * highlighted on every page, as in the chat (index.css).
 *
 * An HTML document shows in an `<iframe sandbox>` that loads
 * `/documents/<owner>/<slug>/artifact`, a route of the server: the frame has
 * an opaque origin, and that route sends the content policy of the document.
 *
 * A document of another member and one that is not there are both 404, so
 * the page says what the viewer said.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Blocks, { span, spans, what, type DocBlock } from "../blocks/Blocks";
import Markdown from "../md/Markdown";
import { When, type DocComment } from "../comments/CommentCard";
import {
  Cards,
  Orphans,
  Plus,
  Resolved,
  SourceLines,
  Unit,
  useGutter,
  type Span,
} from "../comments/Gutter";
import Shell, { embedded, type Session } from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import Fold from "../ui/Fold";
import Spinner from "../ui/Spinner";
import { CommentsBadge, KindChip, StatusChip } from "./Documents";

/** One commit of the file of a document. */
export interface DocVersion {
  sha: string;
  when: string;
  message: string;
}

/** The answer of `GET /api/v1/documents/<owner>/<slug>`. */
export interface DocumentData {
  path: string;
  url: string;
  title: string;
  kind: string;
  status: string;
  format: string;
  summary: string;
  updated: string | null;
  owner: string;
  owner_label: string;
  frontmatter: Record<string, unknown>;
  artifact_url: string | null;
  blocks: DocBlock[];
  report: unknown;
  lines: string[];
  preamble: string[];
  comments: DocComment[];
  versions: DocVersion[];
  version: { sha: string; when: string } | null;
  csrf: string | null;
}

/** The unit of the head: line 0, the document as a whole. */
const HEAD: Span = { first: 0, last: 0 };

/** The properties the chips of the head already give. */
const CHIPPED = new Set(["title", "kind", "status", "updated"]);

/** `3 versions`, `1 version`. */
function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** The time of a commit, as the fold shows it: `2026-11-09 15:00`. */
function stamp(when: string): string {
  return when.slice(0, 16).replace("T", " ");
}

/** One frontmatter value as one line of text. */
function propText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => propText(item)).join(", ");
  }
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

/** True when the body starts with its own title, so the head gives none. */
function bodyHasTitle(lines: string[]): boolean {
  return (lines.find((line) => line.trim() !== "") ?? "").trimStart().startsWith("# ");
}

/** The `title:` line of the frontmatter, which the "+" of line 0 anchors to. */
function titleLine(preamble: string[]): string {
  return preamble.find((line) => line.startsWith("title:")) ?? "";
}

/**
 * The one code block of a body that is mostly code: the document has exactly
 * one code block, and its lines are at least half of the lines of all the
 * blocks. Otherwise null, and the body shows as it is.
 */
export function codeFirst(blocks: DocBlock[]): DocBlock | null {
  const code = blocks.filter((block) => block.kind === "code");
  const only = code[0];
  if (code.length !== 1 || only === undefined) {
    return null;
  }
  const size = (block: DocBlock) => block.last_line - block.first_line + 1;
  const total = blocks.reduce((sum, block) => sum + size(block), 0);
  return size(only) * 2 >= total ? only : null;
}

/** The links of a document: its assignment, and the page it was promoted to. */
function Links({ front }: { front: Record<string, unknown> }) {
  const assignment = propText(front["assignment"]);
  const promoted = propText(front["promoted_to"]);
  return (
    <>
      {/^\d+$/.test(assignment) && (
        <a className="j-chip a-saved" href={`/assignments/${String(Number(assignment))}`}>
          {`assignment ${String(Number(assignment))}`}
        </a>
      )}
      {promoted.startsWith("wiki/") && !promoted.includes("..") && (
        <Link className="j-chip a-saved" to={`/${promoted}`}>
          promoted to the wiki
        </Link>
      )}
    </>
  );
}

/** The properties of the frontmatter that no chip of the head gives. */
function Props({ front }: { front: Record<string, unknown> }) {
  const rows = Object.entries(front).filter(
    ([key, value]) => !CHIPPED.has(key) && propText(value) !== "",
  );
  if (rows.length === 0) {
    return null;
  }
  return (
    <div className="a-props">
      {rows.map(([key, value]) => (
        <div className="a-prop" key={key}>
          <div className="a-unit-body">
            <span className="a-prop-key">{key}</span>
            <span className="a-prop-value a-prop-text">{propText(value)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

/** An HTML document, in the sandboxed frame of the artifact route. */
function Frame({ doc }: { doc: DocumentData }) {
  const scripts = doc.frontmatter["scripts"] === true;
  const note = scripts
    ? "Scripts of this document run in a sandbox, apart from the viewer."
    : "Scripts of this document do not run.";
  return (
    <section className="d-artifact">
      <iframe
        className="d-frame"
        sandbox={scripts ? "allow-scripts" : ""}
        src={doc.artifact_url ?? ""}
        title={doc.title}
        referrerPolicy="no-referrer"
        loading="lazy"
      />
      <p className="j-faint d-frame-note">{note}</p>
    </section>
  );
}

/** The commits of the file, newest first. The newest one is the page itself. */
function Versions({ doc }: { doc: DocumentData }) {
  if (doc.versions.length === 0) {
    return (
      <Fold title="Versions">
        <p className="j-faint">No version history.</p>
      </Fold>
    );
  }
  const current = doc.version?.sha;
  return (
    <Fold title="Versions" hint={count(doc.versions.length, "version")}>
      <ol className="d-versions">
        {doc.versions.map((version, index) => (
          <li key={version.sha}>
            <Link
              to={index === 0 ? doc.url : `${doc.url}?rev=${version.sha}`}
              aria-current={version.sha === current ? "true" : undefined}
            >
              {`${stamp(version.when)} · `}
              <code>{version.sha.slice(0, 8)}</code> {version.message}
              {index === 0 && <span className="j-faint"> (now)</span>}
            </Link>
          </li>
        ))}
      </ol>
    </Fold>
  );
}

/** The note of an older version, which is read-only. */
function Older({ doc }: { doc: DocumentData }) {
  if (doc.version === null) {
    return null;
  }
  return (
    <aside className="a-notice d-old" role="note">
      <strong>{`The version of ${stamp(doc.version.when)}`}</strong>
      <p className="a-text">
        This is an older version, <code>{doc.version.sha.slice(0, 8)}</code>. It is read-only.{" "}
        <Link to={doc.url}>Read the current version</Link>.
      </p>
    </aside>
  );
}

export default function Document() {
  const { owner = "", slug = "" } = useParams();
  const [params] = useSearchParams();
  const rev = params.get("rev") ?? "";
  const embed = params.get("embed") === "1" || embedded();
  const [doc, setDoc] = useState<DocumentData | null>(null);
  const [comments, setComments] = useState<DocComment[]>([]);
  const [person, setPerson] = useState<string>("");
  const [error, setError] = useState<string>("");
  const path = `/documents/${encodeURIComponent(owner)}/${encodeURIComponent(slug)}`;

  useEffect(() => {
    let live = true;
    setDoc(null);
    setError("");
    getJson<DocumentData>(rev === "" ? path : `${path}?rev=${encodeURIComponent(rev)}`)
      .then((answer) => {
        if (live) {
          setDoc(answer);
          setComments(answer.comments);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the document did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [path, rev]);

  useEffect(() => {
    let live = true;
    getJson<Session>("/session")
      .then((answer) => {
        if (live) {
          setPerson(answer.person.id);
        }
      })
      .catch(() => {
        // Only Delete needs to know who signed in: with no session the page
        // still shows the document and its comments.
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    document.title = doc === null ? "document" : doc.title;
  }, [doc]);

  const units = useMemo(() => [HEAD, ...spans(doc?.blocks ?? [])], [doc]);
  const gutter = useGutter({
    url: path,
    lines: doc?.lines ?? [],
    comments,
    csrf: doc?.csrf ?? null,
    person,
    units,
    onComments: setComments,
  });

  if (error !== "") {
    return (
      <Shell section="documents">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (doc === null) {
    return (
      <Shell section="documents">
        <Spinner label="Loading the document" />
      </Shell>
    );
  }
  const headCards = gutter.cards.get(0) ?? [];
  const classes = ["a-hero", "d-hero", "d-unit", headCards.length > 0 ? "has-comments" : ""];
  const head = (
    <header className={classes.filter((part) => part !== "").join(" ")} id="L0">
      <div className="a-chips">
        <StatusChip status={doc.status} />
        <KindChip kind={doc.kind} />
        <Links front={doc.frontmatter} />
        <CommentsBadge open={gutter.open} />
        {doc.updated !== null && (
          <span className="j-meta">
            updated <When at={doc.updated} />
          </span>
        )}
      </div>
      {!bodyHasTitle(doc.lines) && <h1 className="j-doc-title">{doc.title}</h1>}
      <Props front={doc.frontmatter} />
      <Plus gutter={gutter} line={0} text={titleLine(doc.preamble)} what="the document" />
      <Cards gutter={gutter} unit={HEAD} />
    </header>
  );
  const folds = (
    <div className="a-folds">
      <Orphans gutter={gutter} />
      <Fold title="Source" hint={count(doc.lines.length, "line")}>
        <SourceLines gutter={gutter} preamble={doc.preamble} />
      </Fold>
      <Resolved gutter={gutter} />
      <Versions doc={doc} />
    </div>
  );

  if (embed) {
    const code = doc.format === "html" ? null : codeFirst(doc.blocks);
    const rest = code === null ? [] : doc.blocks.filter((block) => block !== code);
    let body;
    if (doc.format === "html") {
      body = <Frame doc={doc} />;
    } else if (code !== null) {
      body = (
        <>
          <div className="a-blocks d-body d-codefirst">
            <Unit
              gutter={gutter}
              unit={span(code)}
              className="a-block code d0"
              what={what(code)}
            >
              <Markdown text={code.markdown} />
            </Unit>
          </div>
          {rest.length > 0 && (
            <Fold title="Notes" hint={count(rest.length, "block")}>
              <Blocks blocks={rest} gutter={gutter} />
            </Fold>
          )}
        </>
      );
    } else {
      body = <Blocks blocks={doc.blocks} gutter={gutter} />;
    }
    return (
      <Shell section="documents">
        <div className="d-embed">
          <Older doc={doc} />
          {body}
          <Fold
            title="Details"
            hint={gutter.open > 0 ? `${count(gutter.open, "open comment")}` : doc.status}
          >
            {head}
            {folds}
          </Fold>
        </div>
      </Shell>
    );
  }
  return (
    <Shell section="documents">
      <nav className="j-crumbs">
        <Link to="/documents">documents</Link> <span aria-hidden="true">/</span>{" "}
        {doc.owner_label}
      </nav>
      {head}
      <Older doc={doc} />
      {doc.format === "html" ? <Frame doc={doc} /> : <Blocks blocks={doc.blocks} gutter={gutter} />}
      {folds}
    </Shell>
  );
}

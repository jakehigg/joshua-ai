/**
 * One comment on a line of a document: the author, when it was written, the
 * body, the mark of a line that changed, the resolution of Joshua when there
 * is one, and Delete on the person's own open comment.
 *
 * The relative time is the rule of `viewer_comments.ago`, so the viewer and
 * this app say the same thing: `just now`, `5m ago`, `2h ago`, `3d ago`,
 * `4mo ago`. A time in the future is `just now`. The full time is in the
 * `title` of the `<time>` element, in the time zone of the browser.
 */

/** One comment, as `GET /api/v1/documents/<owner>/<slug>` sends it. */
export interface DocComment {
  id: string;
  line_no: number;
  line_text: string;
  line_now: string | null;
  stale: boolean;
  body: string;
  author: string;
  created_at: string | null;
  resolved_at: string | null;
  resolution: string | null;
}

/** A past interval as a person reads it, the rule of `viewer_comments.ago`. */
export function ago(seconds: number): string {
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    return `${hours}h ago`;
  }
  const days = Math.floor(hours / 24);
  if (days < 60) {
    return `${days}d ago`;
  }
  return `${Math.floor(days / 30)}mo ago`;
}

/** What line 0, the document as a whole, is called. */
const WHOLE = "The document";

/** `Line 12`, or `The document` for line 0. */
export function where(lineNo: number): string {
  return lineNo === 0 ? WHOLE : `Line ${lineNo}`;
}

/** The time of a comment, relative, with the full time in its title. */
export function When({ at }: { at: string | null }) {
  if (at === null || at === "") {
    return null;
  }
  const when = new Date(at);
  if (Number.isNaN(when.getTime())) {
    return null;
  }
  return (
    <time dateTime={at} title={when.toLocaleString()}>
      {ago((Date.now() - when.getTime()) / 1000)}
    </time>
  );
}

export default function CommentCard({
  comment,
  canDelete = false,
  quote = false,
  onDelete,
}: {
  comment: DocComment;
  canDelete?: boolean;
  /** The comment is on a line of a unit that is not its first line. */
  quote?: boolean;
  onDelete?: (id: string) => void;
}) {
  const resolved = comment.resolved_at !== null;
  const classes = ["a-comment"];
  if (comment.stale) {
    classes.push("stale");
  }
  if (resolved) {
    classes.push("resolved");
  }
  return (
    <div className={classes.join(" ")} id={`C${comment.id}`}>
      {resolved && (
        <p className="j-faint">
          {where(comment.line_no)}: <code>{comment.line_text}</code>
        </p>
      )}
      <p className="a-comment-body">{comment.body}</p>
      {!resolved && comment.stale && (
        <p className="a-was">
          <span className="j-faint">It was on:</span> <code>{comment.line_text}</code>
        </p>
      )}
      {!resolved && !comment.stale && quote && (
        <p className="a-was">
          <span className="j-faint">{where(comment.line_no)}:</span>{" "}
          <code>{comment.line_text}</code>
        </p>
      )}
      {resolved && (
        <p className="a-resolution">
          <span className="j-label">Joshua</span> {comment.resolution ?? ""}{" "}
          <span className="j-faint">
            <When at={comment.resolved_at} />
          </span>
        </p>
      )}
      <div className="a-comment-foot">
        <span className="j-faint">{comment.author}</span>
        <span className="j-faint">
          <When at={comment.created_at} />
        </span>
        {comment.stale && <span className="j-badge warn">line changed</span>}
        {!resolved && canDelete && onDelete !== undefined && (
          <button
            className="j-btn sm v-del"
            type="button"
            onClick={() => {
              onDelete(comment.id);
            }}
          >
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

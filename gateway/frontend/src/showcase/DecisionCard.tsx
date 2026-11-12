/**
 * One decision of a report, numbered, with the way to answer it.
 *
 * On the report of an assignment (`answers`) the card takes an answer: a
 * textarea and "Send to Joshua", which stores a comment on the first line of
 * the decision. The card then shows that answer, with "Sent to Joshua" and
 * Delete, or the resolution of Joshua when the manager resolved it, and the
 * card is marked as answered.
 *
 * On a document the card has a copy button instead: it copies the text the
 * server built (`copy`) with `navigator.clipboard`, so a person pastes the
 * decision into a conversation with Joshua. That button replaces the inline
 * script the server page used, and the viewer now has no inline script at
 * all. The button says "Copied" once it copied, as the copy button of a code
 * block does.
 */

import { useState } from "react";

import { When, type DocComment } from "../comments/CommentCard";
import { MAX_COMMENT, type Gutter, type Span } from "../comments/Gutter";
import Markdown from "../md/Markdown";
import type { ReportItem } from "../assignments/data";
import Card from "./Card";

/** The words on the answer of a decision. */
export const SENT = "Sent to Joshua";
export const SEND = "Send to Joshua";

/** The lines of the text one decision holds. */
export function span(item: ReportItem): Span {
  return { first: item.first_line, last: item.last_line };
}

/** The answers of the person to one decision, and the resolution of Joshua. */
function Answers({
  gutter,
  open,
  resolved,
}: {
  gutter: Gutter;
  open: DocComment[];
  resolved: DocComment[];
}) {
  return (
    <>
      {resolved.map((comment) => (
        <div className="a-answer resolved" id={`C${comment.id}`} key={comment.id}>
          <p className="a-answer-body">{comment.body}</p>
          <p className="a-resolution">
            <span className="j-label">Joshua</span> {comment.resolution ?? ""}{" "}
            <span className="j-faint">
              <When at={comment.resolved_at} />
            </span>
          </p>
        </div>
      ))}
      {open.map((comment) => (
        <div className="a-answer" id={`C${comment.id}`} key={comment.id}>
          <p className="a-answer-body">{comment.body}</p>
          <div className="a-comment-foot">
            <span className="j-badge ok">{SENT}</span>
            <span className="j-faint">
              <When at={comment.created_at} />
            </span>
            {gutter.csrf !== null && comment.author === gutter.person && (
              <button
                className="j-btn sm v-del"
                type="button"
                onClick={() => {
                  void gutter.remove(comment.id);
                }}
              >
                Delete
              </button>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

/** The form of an answer to one decision, on the first line of the decision. */
function AnswerForm({ gutter, item }: { gutter: Gutter; item: ReportItem }) {
  const [body, setBody] = useState("");
  const line = item.first_line;
  return (
    <form
      className="a-answer-form"
      onSubmit={(event) => {
        event.preventDefault();
        void gutter.add(line, gutter.lines[line - 1] ?? "", body).then(() => {
          setBody("");
        });
      }}
    >
      <div className="j-field">
        <textarea
          rows={3}
          maxLength={MAX_COMMENT}
          required
          aria-label={`Your answer to decision ${String(item.number)}`}
          placeholder="Your answer"
          value={body}
          onChange={(event) => {
            setBody(event.target.value);
          }}
        />
      </div>
      <button className="j-btn sm primary" type="submit" disabled={gutter.busy}>
        {SEND}
      </button>
    </form>
  );
}

/** The copy button of a decision of a document. */
function Copy({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="a-decision-foot">
      <span className="j-faint">Tell Joshua your answer.</span>
      <button
        className="j-btn sm a-copy"
        type="button"
        onClick={() => {
          void navigator.clipboard?.writeText(text).catch(() => undefined);
          setCopied(true);
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export default function DecisionCard({
  item,
  gutter,
  answers = false,
}: {
  item: ReportItem;
  gutter: Gutter;
  /** The report of an assignment: the card takes an answer. */
  answers?: boolean;
}) {
  const unit = span(item);
  const open = answers ? (gutter.cards.get(unit.first) ?? []) : [];
  const resolved = answers
    ? gutter.resolved.filter(
        (comment) => comment.line_no >= unit.first && comment.line_no <= unit.last,
      )
    : [];
  const answered = open.length > 0 || resolved.length > 0;
  return (
    <Card
      gutter={gutter}
      unit={unit}
      className={answered ? "a-decision answered" : "a-decision"}
      what={`decision ${String(item.number)}`}
      comments={!answers}
    >
      <span className="a-n" aria-hidden="true">
        {item.number}
      </span>
      <div className="a-decision-main">
        <p className="a-q">{item.question}</p>
        {item.context !== "" && (
          <div className="a-ctx">
            <Markdown text={item.context} />
          </div>
        )}
        {answers ? (
          <div className="a-decision-answer">
            <Answers gutter={gutter} open={open} resolved={resolved} />
            {open.length === 0 && gutter.csrf !== null && (
              <AnswerForm gutter={gutter} item={item} />
            )}
          </div>
        ) : (
          item.copy !== "" && <Copy text={item.copy} />
        )}
      </div>
    </Card>
  );
}

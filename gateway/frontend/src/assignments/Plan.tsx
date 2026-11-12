/**
 * The plan of an assignment: how far it is, and the plan itself.
 *
 * The server counts the task boxes (`viewer_report.plan_progress`) and sends
 * the count and the next step, so the page draws the bar and says what comes
 * next without counting anything. The plan renders over the block renderer,
 * one block a unit of its own gutter on the `plan` text, and a task box
 * becomes a box of a checklist.
 */

import { useMemo } from "react";

import { span, spans, what, type DocBlock } from "../blocks/Blocks";
import type { DocComment } from "../comments/CommentCard";
import { Orphans, Resolved, Unit, useGutter } from "../comments/Gutter";
import Markdown from "../md/Markdown";
import ErrorLine from "../ui/ErrorLine";
import type { PlanData } from "./data";

/** A task box at the start of a list item: `- [x] read the pages`. */
const TASK = /^(\s*[-*+]\s+)\[([ xX])\]\s*/;

/** One task box: whether it is done, and the text after the box. */
export function task(block: DocBlock): { done: boolean; text: string } | null {
  if (block.kind !== "li") {
    return null;
  }
  const match = TASK.exec(block.markdown);
  if (match === null) {
    return null;
  }
  return { done: (match[2] ?? " ") !== " ", text: block.markdown.slice(match[0].length) };
}

/** The bar of the progress, as a share of the steps that are done. */
function Meter({ percent }: { percent: number }) {
  return (
    <svg className="a-meter" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">
      <rect className="track" width="100" height="6" rx="3" />
      <rect className="fill" width={percent} height="6" rx="3" />
    </svg>
  );
}

/** The steps that are done, the bar, and the next step. */
export function Progress({ plan }: { plan: PlanData }) {
  const { done, total, next, percent } = plan.progress;
  if (total === 0 && next === "") {
    return null;
  }
  return (
    <div className="a-now" aria-label="Progress of the plan">
      {total > 0 && (
        <div className="a-progress">
          <span className="a-steps">
            <b>{done}</b>
            {` of ${String(total)} step${total === 1 ? "" : "s"}`}
          </span>
          <Meter percent={percent} />
        </div>
      )}
      {next !== "" && (
        <p className="a-now-line">
          <span className="j-label">Next</span>
          <span>{next}</span>
        </p>
      )}
    </div>
  );
}

export default function Plan({
  url,
  plan,
  comments,
  csrf,
  person,
  onComments,
  reload,
}: {
  url: string;
  plan: PlanData;
  comments: DocComment[];
  csrf: string | null;
  person: string;
  onComments: (next: DocComment[]) => void;
  reload: () => void;
}) {
  const units = useMemo(() => spans(plan.blocks), [plan]);
  const gutter = useGutter({
    url,
    lines: plan.lines,
    comments,
    csrf,
    person,
    units,
    onComments,
    prefix: "P",
    fields: { target: "plan" },
    reload,
  });

  function draw(block: DocBlock) {
    const box = task(block);
    return (
      <Unit
        key={block.id}
        gutter={gutter}
        unit={span(block)}
        className={`a-block ${block.kind} d${String(block.depth)}`}
        what={what(block)}
      >
        {box === null ? (
          <Markdown text={block.markdown} />
        ) : (
          <ul className="a-checklist">
            <li className={box.done ? "done" : undefined}>
              <span className="a-box" role="img" aria-label={box.done ? "done" : "to do"} />
              <Markdown text={box.text} />
            </li>
          </ul>
        )}
      </Unit>
    );
  }

  return (
    <section className="a-section" id="plan">
      <div className="a-head">
        <h2>Plan</h2>
        {plan.updated !== "" && <span className="j-meta">{`updated ${plan.updated}`}</span>}
      </div>
      <Progress plan={plan} />
      {plan.blocks.length === 0 ? (
        <p className="j-faint">No plan yet.</p>
      ) : (
        <div className="a-blocks">{plan.blocks.map(draw)}</div>
      )}
      {gutter.error !== "" && <ErrorLine text={gutter.error} />}
      <Orphans gutter={gutter} />
      <Resolved gutter={gutter} />
    </section>
  );
}

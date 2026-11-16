/**
 * The plan of an assignment: how far it is, and the plan itself.
 *
 * The server counts the task boxes (`viewer_report.plan_progress`) and sends
 * the count and the next step, so the page draws the bar and says what comes
 * next without counting anything. The plan renders over the block renderer,
 * one block a unit of its own gutter on the `plan` text, and a task box
 * becomes a box of a checklist. A box of a step that the worker marked
 * skipped (`- [-] step — skipped: <reason>`) is its own mark, and the reason
 * is in the text of the step.
 *
 * An assignment that ended with open steps shows "ended, N steps open" in
 * place of the bar and the next step: the run is over, so a bar that stands
 * still would say that the work goes on.
 */

import { useMemo } from "react";

import { span, spans, what, type DocBlock } from "../blocks/Blocks";
import type { DocComment } from "../comments/CommentCard";
import { Orphans, Resolved, Unit, useGutter } from "../comments/Gutter";
import Markdown from "../md/Markdown";
import ErrorLine from "../ui/ErrorLine";
import type { PlanData } from "./data";

/** A task box at the start of a list item: `- [x] read the pages`. */
const TASK = /^(\s*[-*+]\s+)\[([ xX~-])\]\s*/;

/** One task box: how far the step is, and the text after the box. */
export function task(block: DocBlock): { done: boolean; skipped: boolean; text: string } | null {
  if (block.kind !== "li") {
    return null;
  }
  const match = TASK.exec(block.markdown);
  if (match === null) {
    return null;
  }
  const mark = match[2] ?? " ";
  return {
    done: mark === "x" || mark === "X",
    skipped: mark === "-" || mark === "~",
    text: block.markdown.slice(match[0].length),
  };
}

/** What the mark of a box says: one class name, and one label. */
function boxState(box: { done: boolean; skipped: boolean }): { name: string; label: string } {
  if (box.done) {
    return { name: "done", label: "done" };
  }
  if (box.skipped) {
    return { name: "skipped", label: "skipped" };
  }
  return { name: "", label: "to do" };
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

/**
 * The steps that are done, the bar, and the next step. `ended` is true when
 * the run is over: with a step still open it then says so in place of both.
 */
export function Progress({ plan, ended = false }: { plan: PlanData; ended?: boolean }) {
  const { done, total, skipped, open, next, percent } = plan.progress;
  if (total === 0 && next === "") {
    return null;
  }
  const left = ended && open > 0;
  return (
    <div className="a-now" aria-label="Progress of the plan">
      {left && (
        <p className="a-open-steps">{`ended, ${String(open)} step${open === 1 ? "" : "s"} open`}</p>
      )}
      {!left && total > 0 && (
        <div className="a-progress">
          <span className="a-steps">
            <b>{done}</b>
            {` of ${String(total)} step${total === 1 ? "" : "s"}`}
            {skipped > 0 && `, ${String(skipped)} skipped`}
          </span>
          <Meter percent={percent} />
        </div>
      )}
      {!left && next !== "" && (
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
  ended = false,
}: {
  url: string;
  plan: PlanData;
  comments: DocComment[];
  csrf: string | null;
  person: string;
  onComments: (next: DocComment[]) => void;
  reload: () => void;
  ended?: boolean;
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
    const state = box === null ? null : boxState(box);
    return (
      <Unit
        key={block.id}
        gutter={gutter}
        unit={span(block)}
        className={`a-block ${block.kind} d${String(block.depth)}`}
        what={what(block)}
      >
        {box === null || state === null ? (
          <Markdown text={block.markdown} />
        ) : (
          <ul className="a-checklist">
            <li className={state.name === "" ? undefined : state.name}>
              <span className="a-box" role="img" aria-label={state.label} />
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
      <Progress plan={plan} ended={ended} />
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

/**
 * The definition of an assignment, as a document.
 *
 * The server cuts the text of the manager into units
 * (`viewer_assignments.definition_units`): each field of the YAML is a
 * property row, and the goal is a head and one unit a block of its Markdown.
 * Each unit carries its first and its last line of the `definition` text, so
 * it is a unit of the comment gutter: its "+" writes a comment on its first
 * line, and the open comments on any of its lines show under it. The `Source`
 * fold gives every line with its own "+".
 *
 * A manager that sends no `definition` text counts no line, so the page shows
 * the spec as source and no line takes a comment (`csrf` is null).
 */

import { useMemo } from "react";

import { CommentsBadge } from "../pages/Documents";
import { Orphans, Resolved, SourceLines, Unit, useGutter, type Span } from "../comments/Gutter";
import type { DocComment } from "../comments/CommentCard";
import Markdown from "../md/Markdown";
import Fold from "../ui/Fold";
import ErrorLine from "../ui/ErrorLine";
import type { DefinitionData, DefinitionUnit, PropValue } from "./data";

/** The lines one unit holds, as the gutter reads a unit. */
export function span(unit: DefinitionUnit): Span {
  return { first: unit.first_line, last: unit.last_line };
}

/** The class of one unit: a property, the head of the goal, or a block. */
export function unitClass(unit: DefinitionUnit): string {
  if (unit.kind === "prop") {
    return "a-prop";
  }
  if (unit.kind === "goal") {
    return "a-goal-head";
  }
  return `a-block ${unit.kind} d${unit.depth}`;
}

/** The value of one field: text, tags, the source lines, or none. */
function Value({ value }: { value: PropValue | null }) {
  if (value === null || value.kind === "none") {
    return <span className="j-faint">none</span>;
  }
  if (value.kind === "code") {
    return <code className="a-code">{value.text ?? ""}</code>;
  }
  if (value.kind === "tags") {
    return (
      <span className="a-tags">
        {(value.tags ?? []).map((tag, index) => (
          <span className="a-tag" key={`${tag}-${String(index)}`}>
            {tag}
          </span>
        ))}
      </span>
    );
  }
  return <span className="a-prop-text">{value.text ?? ""}</span>;
}

/** The inside of one unit. */
function Body({ unit }: { unit: DefinitionUnit }) {
  if (unit.kind === "goal") {
    return <span className="a-prop-key">goal</span>;
  }
  if (unit.kind === "prop") {
    return (
      <>
        <span className="a-prop-key">{unit.label}</span>
        <span className="a-prop-value">
          <Value value={unit.value} />
        </span>
      </>
    );
  }
  return <Markdown text={unit.markdown} />;
}

/** `4 lines`, `1 line`. */
function count(n: number, noun: string): string {
  return `${String(n)} ${noun}${n === 1 ? "" : "s"}`;
}

export default function Definition({
  url,
  definition,
  comments,
  csrf,
  person,
  onComments,
  reload,
  open = false,
}: {
  url: string;
  definition: DefinitionData;
  comments: DocComment[];
  csrf: string | null;
  person: string;
  onComments: (next: DocComment[]) => void;
  reload: () => void;
  open?: boolean;
}) {
  const units = useMemo(() => definition.units.map(span), [definition]);
  const gutter = useGutter({
    url,
    lines: definition.lines,
    comments,
    csrf,
    person,
    units,
    onComments,
    prefix: "L",
    fields: { target: "definition" },
    reload,
  });
  const props = definition.units.filter((unit) => unit.kind === "prop");
  const goal = definition.units.filter((unit) => unit.kind !== "prop");

  function draw(unit: DefinitionUnit) {
    return (
      <Unit key={unit.id} gutter={gutter} unit={span(unit)} className={unitClass(unit)} what={unit.what}>
        <Body unit={unit} />
      </Unit>
    );
  }

  return (
    <details className="a-def" open={open || gutter.open > 0} id="definition">
      <summary>
        Definition <CommentsBadge open={gutter.open} />
      </summary>
      <div className="a-doc">
        {props.length > 0 && <div className="a-props">{props.map(draw)}</div>}
        {goal.length > 0 && <div className="a-goal j-prose">{goal.map(draw)}</div>}
        {definition.units.length === 0 &&
          (definition.lines.length === 0 ? (
            <p className="j-faint a-def-empty">The definition is empty.</p>
          ) : (
            <pre>
              <code>{definition.lines.join("\n")}</code>
            </pre>
          ))}
      </div>
      {gutter.error !== "" && <ErrorLine text={gutter.error} />}
      <Orphans gutter={gutter} />
      <Fold title="Source" hint={count(definition.lines.length, "line")}>
        <SourceLines gutter={gutter} />
      </Fold>
      <Resolved gutter={gutter} />
    </details>
  );
}

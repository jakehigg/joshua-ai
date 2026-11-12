/**
 * One card of a showcase, as a unit of the comment gutter.
 *
 * A highlight, a decision, an output and a follow-up are each a card, and
 * each one holds some lines of the text: its "+" writes a comment on its
 * first line, and the open comments on any of its lines show under it. This
 * is `viewer_showcase._card`, which put the body straight into the `<li>`,
 * so the layout of each kind of card is the layout of `viewer.css`.
 *
 * With `comments` false the card shows the comments of its lines itself, as
 * a decision of an assignment shows the answers of the person, and then it
 * has no "+": the answer form is the way to write one.
 */

import type { ReactNode } from "react";

import { Cards, Plus, type Gutter, type Span } from "../comments/Gutter";

export default function Card({
  gutter,
  unit,
  className = "",
  what,
  comments = true,
  children,
}: {
  gutter: Gutter;
  unit: Span;
  className?: string;
  what: string;
  comments?: boolean;
  children: ReactNode;
}) {
  const open = gutter.cards.get(unit.first) ?? [];
  const classes = [
    className,
    "a-card-unit",
    comments && open.length > 0 ? "has-comments" : "",
  ];
  const anchors: ReactNode[] = [];
  for (let line = unit.first + 1; line <= unit.last; line += 1) {
    anchors.push(<span className="a-anchor" key={line} id={`${gutter.prefix}${line}`} />);
  }
  return (
    <li
      className={classes.filter((part) => part !== "").join(" ")}
      id={`${gutter.prefix}${unit.first}`}
    >
      {anchors}
      {children}
      {comments && (
        <>
          <Plus
            gutter={gutter}
            line={unit.first}
            text={gutter.lines[unit.first - 1] ?? ""}
            what={what}
          />
          <Cards gutter={gutter} unit={unit} />
        </>
      )}
    </li>
  );
}

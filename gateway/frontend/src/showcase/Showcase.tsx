/**
 * A report as a showcase: the shape the server reads, drawn as cards.
 *
 * The server parses one Markdown text with the headings of a report
 * (`viewer_report`) and sends text and numbers, never HTML
 * (`viewer_api.assignments.report_json`). This is what the viewer rendered
 * in `viewer_showcase`, and the rules are the same: the highlights as cards,
 * the decisions as numbered cards, the outputs as cards grouped by kind,
 * what was asked as a quote, what was done as prose, and the follow-ups as a
 * checklist. A section whose body says nothing is not shown.
 *
 * Each card, each heading and each block of prose is a unit of the comment
 * gutter, anchored to its line of the text, so a comment lands on the line of
 * the card a person is reading. `showcaseSpans` gives those units in the
 * order of the page, which is the order a comment is placed in.
 *
 * A text with none of the headings (`structured` false) is not a showcase:
 * the page shows its blocks, as plain Markdown.
 *
 * The report of an assignment takes an answer on each decision (`answers`);
 * a document offers the copy button instead (`DecisionCard`).
 */

import { span, what, type DocBlock } from "../blocks/Blocks";
import { Cards, Plus, Unit, type Gutter, type Span } from "../comments/Gutter";
import Markdown from "../md/Markdown";
import {
  ASKED,
  COST,
  DECISIONS,
  DONE,
  FOLLOW_UPS,
  HIGHLIGHTS,
  OUTPUTS,
  type ReportItem,
  type ReportOutput,
  type ReportSection,
  type ShowcaseData,
} from "../assignments/data";
import Card from "./Card";
import DecisionCard, { span as decisionSpan } from "./DecisionCard";
import OutputCard from "./OutputCard";

/** A body that says nothing: blank, `None.`, `No decisions.`, `-`. The rule
 * of `viewer_report.is_none`. */
const NONE = /^(?:none(?: recorded| yet)?|nothing|n\/?a|no\b[^\n]{0,60}|-+)\.?$/i;

export function isNone(text: string): boolean {
  const body = text.trim();
  return body === "" || NONE.test(body);
}

/** `3 decisions`, `1 decision`. */
export function count(n: number, noun: string): string {
  return `${String(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/** One section of the report, or null when it has no such heading. */
export function part(report: ShowcaseData, key: string): ReportSection | null {
  return report.sections[key] ?? null;
}

/** The lines of the heading of a section, up to the first line of its body. */
export function headSpan(section: ReportSection): Span {
  return { first: section.line, last: Math.max(section.line, section.start - 1) };
}

/** The lines of one item of a section. */
function itemSpan(item: ReportItem | ReportOutput): Span {
  return { first: item.first_line, last: item.last_line };
}

/** The decisions of the report, which the tiles of a page also count. */
export function decisions(report: ShowcaseData | null): ReportItem[] {
  const found = report === null ? null : part(report, DECISIONS);
  return found === null ? [] : found.items;
}

/** The outputs the Outputs section names. */
export function outputs(report: ShowcaseData | null): ReportOutput[] {
  const found = report === null ? null : part(report, OUTPUTS);
  return found === null ? [] : found.outputs;
}

/** True when a section is worth a card of its own. */
function shown(section: ReportSection | null): section is ReportSection {
  return section !== null && !section.empty;
}

/**
 * The units of the showcase, in the order of the page.
 *
 * A comment lands on the first unit that holds its line, so this order is
 * the order the sections are drawn in. A text that is no showcase has the
 * units of its blocks.
 */
export function showcaseSpans(report: ShowcaseData | null, blocks: DocBlock[]): Span[] {
  if (report === null || !report.structured) {
    return blocks.map(span);
  }
  const units: Span[] = [];
  const section = (key: string, inner: (found: ReportSection) => Span[]) => {
    const found = part(report, key);
    if (shown(found)) {
      units.push(headSpan(found), ...inner(found));
    }
  };
  section(HIGHLIGHTS, (found) => found.items.map(itemSpan));
  section(DECISIONS, (found) => found.items.map(itemSpan));
  section(OUTPUTS, (found) => found.outputs.map(itemSpan));
  section(ASKED, (found) => found.blocks.map(span));
  section(DONE, (found) => found.blocks.map(span));
  units.push(...(report.preamble?.blocks ?? []).map(span));
  for (const extra of report.extra) {
    units.push(headSpan(extra), ...extra.blocks.map(span));
  }
  section(FOLLOW_UPS, (found) => found.items.map(itemSpan));
  section(COST, (found) => found.blocks.map(span));
  return units;
}

/** The blocks of a text, each one a unit of the gutter. */
function BlockUnits({ blocks, gutter }: { blocks: DocBlock[]; gutter: Gutter }) {
  return (
    <>
      {blocks.map((block) => (
        <Unit
          key={block.id}
          gutter={gutter}
          unit={span(block)}
          className={`a-block ${block.kind} d${String(block.depth)}`}
          what={what(block)}
        >
          <Markdown text={block.markdown} />
        </Unit>
      ))}
    </>
  );
}

/** The heading of a section, with its "+" and the comments on its lines. */
function Head({
  gutter,
  section,
  title,
  badge = "",
}: {
  gutter: Gutter;
  section: ReportSection;
  title: string;
  badge?: string;
}) {
  const unit = headSpan(section);
  return (
    <>
      <div className="a-head" id={unit.first > 0 ? `${gutter.prefix}${unit.first}` : undefined}>
        <h2>{title}</h2>
        {badge !== "" && <span className="a-count">{badge}</span>}
        {unit.first > 0 && (
          <Plus
            gutter={gutter}
            line={unit.first}
            text={gutter.lines[unit.first - 1] ?? ""}
            what="the heading"
          />
        )}
      </div>
      <Cards gutter={gutter} unit={unit} />
    </>
  );
}

/** The text of a section that is not in one of its list items. */
function Rest({ section }: { section: ReportSection }) {
  if (isNone(section.rest)) {
    return null;
  }
  return <Markdown text={section.rest} />;
}

/** The highlights, each one a card: the bold lead, the sentence under it. */
function Highlights({ gutter, section }: { gutter: Gutter; section: ReportSection }) {
  return (
    <section className="a-highlights" aria-label="Highlights">
      <Cards gutter={gutter} unit={headSpan(section)} />
      {section.items.length > 0 && (
        <ul className="a-cards a-hls">
          {section.items.map((item) => (
            <Card
              key={item.first_line}
              gutter={gutter}
              unit={itemSpan(item)}
              className="a-hl"
              what="the highlight"
            >
              {item.lead === "" ? (
                <div className="a-hl-text">
                  <Markdown text={item.text} />
                </div>
              ) : (
                <>
                  <strong className="a-hl-lead">{item.lead}</strong>
                  {item.body !== "" && (
                    <div className="a-hl-text">
                      <Markdown text={item.body} />
                    </div>
                  )}
                </>
              )}
            </Card>
          ))}
        </ul>
      )}
      <Rest section={section} />
    </section>
  );
}

/** The decisions, numbered, each one with the way to answer it. */
function Decisions({
  gutter,
  section,
  answers,
}: {
  gutter: Gutter;
  section: ReportSection;
  answers: boolean;
}) {
  return (
    <section className="a-section a-decide" id="decisions">
      <Head
        gutter={gutter}
        section={section}
        title="Decisions for you"
        badge={section.items.length > 0 ? count(section.items.length, "decision") : ""}
      />
      {section.items.length > 0 && (
        <ol className="a-decisions">
          {section.items.map((item) => (
            <DecisionCard
              key={decisionSpan(item).first}
              item={item}
              gutter={gutter}
              answers={answers}
            />
          ))}
        </ol>
      )}
      <Rest section={section} />
    </section>
  );
}

/** The outputs as cards, grouped by kind, in the order each kind first comes. */
function Outputs({ gutter, section }: { gutter: Gutter; section: ReportSection }) {
  const groups = new Map<string, ReportOutput[]>();
  for (const output of section.outputs) {
    const key = output.kind.trim().toLowerCase();
    groups.set(key, [...(groups.get(key) ?? []), output]);
  }
  return (
    <section className="a-section" id="outputs">
      <Head
        gutter={gutter}
        section={section}
        title="Outputs"
        badge={count(section.outputs.length, "output")}
      />
      {[...groups.values()].map((group) => (
        <div className="a-group" key={group[0]?.first_line}>
          <h3 className="j-label">
            {group[0]?.kind.trim() === "" ? "other" : group[0]?.kind.trim()}{" "}
            <span className="j-count">{group.length}</span>
          </h3>
          <ul className="a-cards a-outs">
            {group.map((output) => (
              <Card
                key={output.first_line}
                gutter={gutter}
                unit={itemSpan(output)}
                className="a-out"
                what="the output"
              >
                <OutputCard output={output} />
              </Card>
            ))}
          </ul>
        </div>
      ))}
      <Rest section={section} />
    </section>
  );
}

/** What was asked, as a quote. */
function Asked({ gutter, section }: { gutter: Gutter; section: ReportSection }) {
  return (
    <section className="a-section" id="asked">
      <Head gutter={gutter} section={section} title="What was asked" />
      <blockquote className="a-quote a-blocks">
        <BlockUnits blocks={section.blocks} gutter={gutter} />
      </blockquote>
    </section>
  );
}

/**
 * What was done, as prose: the section itself, then the text before the first
 * heading, then each heading the report shape does not know.
 */
function Done({ gutter, report }: { gutter: Gutter; report: ShowcaseData }) {
  const section = part(report, DONE);
  const preamble = report.preamble;
  if (!shown(section) && preamble === null && report.extra.length === 0) {
    return null;
  }
  return (
    <section className="a-section" id="done">
      {shown(section) ? (
        <Head gutter={gutter} section={section} title="What was done" />
      ) : (
        <div className="a-head">
          <h2>What was done</h2>
        </div>
      )}
      <div className="a-blocks">
        {shown(section) && <BlockUnits blocks={section.blocks} gutter={gutter} />}
        {preamble !== null && <BlockUnits blocks={preamble.blocks} gutter={gutter} />}
        {report.extra.map((extra) => (
          <div key={extra.line}>
            <Unit
              gutter={gutter}
              unit={headSpan(extra)}
              className="a-block h d0"
              what="the heading"
            >
              <h2>{extra.title}</h2>
            </Unit>
            <BlockUnits blocks={extra.blocks} gutter={gutter} />
          </div>
        ))}
      </div>
    </section>
  );
}

/** The follow-ups, as a checklist nobody ticks: Joshua does the work. */
function FollowUps({ gutter, section }: { gutter: Gutter; section: ReportSection }) {
  return (
    <section className="a-section" id="follow-ups">
      <Head gutter={gutter} section={section} title="Follow-ups" />
      {section.items.length > 0 && (
        <ul className="a-checklist">
          {section.items.map((item) => (
            <Card
              key={item.first_line}
              gutter={gutter}
              unit={itemSpan(item)}
              what="the follow-up"
            >
              <span className="a-box" aria-hidden="true" />
              <Markdown text={item.text} />
            </Card>
          ))}
        </ul>
      )}
      <Rest section={section} />
    </section>
  );
}

/** A section shown as its blocks, such as the Cost. */
function Prose({
  gutter,
  section,
  title,
  id,
}: {
  gutter: Gutter;
  section: ReportSection;
  title: string;
  id: string;
}) {
  return (
    <section className="a-section" id={id}>
      <Head gutter={gutter} section={section} title={title} />
      <div className="a-blocks">
        <BlockUnits blocks={section.blocks} gutter={gutter} />
      </div>
    </section>
  );
}

export default function Showcase({
  report,
  blocks = [],
  gutter,
  answers = false,
}: {
  report: ShowcaseData | null;
  /** The blocks of the whole text, which a text that is no showcase shows. */
  blocks?: DocBlock[];
  gutter: Gutter;
  /** The report of an assignment: each decision takes an answer. */
  answers?: boolean;
}) {
  if (report === null || !report.structured) {
    return (
      <div className="a-blocks d-body">
        <BlockUnits blocks={blocks} gutter={gutter} />
      </div>
    );
  }
  const highlights = part(report, HIGHLIGHTS);
  const decided = part(report, DECISIONS);
  const outs = part(report, OUTPUTS);
  const asked = part(report, ASKED);
  const followUps = part(report, FOLLOW_UPS);
  const cost = part(report, COST);
  return (
    <div className="a-showcase">
      {shown(highlights) && <Highlights gutter={gutter} section={highlights} />}
      {shown(decided) && (
        <Decisions gutter={gutter} section={decided} answers={answers} />
      )}
      {shown(outs) && <Outputs gutter={gutter} section={outs} />}
      {shown(asked) && <Asked gutter={gutter} section={asked} />}
      <Done gutter={gutter} report={report} />
      {shown(followUps) && <FollowUps gutter={gutter} section={followUps} />}
      {shown(cost) && <Prose gutter={gutter} section={cost} title="Cost" id="cost" />}
    </div>
  );
}

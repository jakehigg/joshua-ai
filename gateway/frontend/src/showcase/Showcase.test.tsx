import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Showcase, { headSpan, isNone, showcaseSpans } from "./Showcase";
import { SEND } from "./DecisionCard";
import type {
  ReportItem,
  ReportOutput,
  ReportSection,
  ShowcaseData,
} from "../assignments/data";
import type { DocBlock } from "../blocks/Blocks";
import type { DocComment } from "../comments/CommentCard";
import type { Gutter } from "../comments/Gutter";

/** The text the sample report is parsed from, one string a line. */
const LINES = [
  "# Party plan",
  "",
  "Twenty people come on Saturday.",
  "",
  "## Highlights",
  "",
  "- **The tent is booked.** It holds twenty.",
  "- The food is ordered.",
  "",
  "## What was asked",
  "",
  "Plan the party for Saturday.",
  "",
  "## What was done",
  "",
  "Called the tent place and the baker.",
  "",
  "## Outputs",
  "",
  "- document: documents/alex/party.md — Party plan",
  "- issue: https://git.example.com/joshua/issues/12 — Issue 12",
  "",
  "## Decisions for you",
  "",
  "1. **Buy the big tent?** It costs 200.",
  "2. **Hire a cook?** Nobody wants to cook.",
  "",
  "## Follow-ups",
  "",
  "- Pay the baker on Friday.",
  "",
  "## Cost",
  "",
  "12,000 tokens over 14 turns.",
];

function block(first: number, kind: string, markdown: string, last = first): DocBlock {
  return { id: `R${String(first)}`, kind, depth: 0, markdown, first_line: first, last_line: last };
}

function item(fields: Partial<ReportItem> & { number: number }): ReportItem {
  return {
    text: "",
    lead: "",
    body: "",
    line: 0,
    first_line: 0,
    last_line: 0,
    question: "",
    context: "",
    copy: "",
    ...fields,
  };
}

function section(fields: Partial<ReportSection> & { key: string }): ReportSection {
  return {
    title: "",
    body: "",
    line: 0,
    start: 0,
    end: 0,
    empty: false,
    items: [],
    outputs: [],
    rest: "",
    blocks: [],
    ...fields,
  };
}

const OUTPUTS: ReportOutput[] = [
  {
    kind: "document",
    icon: "page",
    ref: "documents/alex/party.md",
    title: "Party plan",
    note: "",
    url: "/documents/alex/party",
    line: 0,
    first_line: 20,
    last_line: 20,
  },
  {
    kind: "issue",
    icon: "issue",
    ref: "https://git.example.com/joshua/issues/12",
    title: "Issue 12",
    note: "",
    url: "https://git.example.com/joshua/issues/12",
    line: 1,
    first_line: 21,
    last_line: 22,
  },
];

/** The sample report, as `report_json` sends it. */
const REPORT: ShowcaseData = {
  structured: true,
  title: "Party plan",
  lede: "Twenty people come on Saturday.",
  generated: false,
  first_section: 5,
  preamble: null,
  turns: 14,
  extra: [],
  sections: {
    highlights: section({
      key: "highlights",
      title: "Highlights",
      line: 5,
      start: 7,
      end: 9,
      items: [
        item({
          number: 1,
          text: "**The tent is booked.** It holds twenty.",
          lead: "The tent is booked.",
          body: "It holds twenty.",
          first_line: 7,
          last_line: 7,
        }),
        item({
          number: 2,
          text: "The food is ordered.",
          line: 1,
          first_line: 8,
          last_line: 9,
        }),
      ],
      blocks: [block(7, "li", "- **The tent is booked.** It holds twenty.")],
    }),
    "what was asked": section({
      key: "what was asked",
      title: "What was asked",
      body: "Plan the party for Saturday.",
      line: 10,
      start: 12,
      end: 13,
      rest: "Plan the party for Saturday.",
      blocks: [block(12, "p", "Plan the party for Saturday.")],
    }),
    "what was done": section({
      key: "what was done",
      title: "What was done",
      body: "Called the tent place and the baker.",
      line: 14,
      start: 16,
      end: 17,
      rest: "Called the tent place and the baker.",
      blocks: [block(16, "p", "Called the tent place and the baker.")],
    }),
    outputs: section({
      key: "outputs",
      title: "Outputs",
      line: 18,
      start: 20,
      end: 22,
      outputs: OUTPUTS,
      blocks: [block(20, "li", "- document: documents/alex/party.md — Party plan")],
    }),
    "decisions for you": section({
      key: "decisions for you",
      title: "Decisions for you",
      line: 23,
      start: 25,
      end: 27,
      items: [
        item({
          number: 1,
          text: "**Buy the big tent?** It costs 200.",
          lead: "Buy the big tent?",
          body: "It costs 200.",
          question: "Buy the big tent?",
          context: "It costs 200.",
          first_line: 25,
          last_line: 25,
          copy: "Document documents/alex/party.md, decision 1: Buy the big tent?",
        }),
        item({
          number: 2,
          text: "**Hire a cook?** Nobody wants to cook.",
          lead: "Hire a cook?",
          body: "Nobody wants to cook.",
          question: "Hire a cook?",
          context: "Nobody wants to cook.",
          line: 1,
          first_line: 26,
          last_line: 27,
          copy: "Document documents/alex/party.md, decision 2: Hire a cook?",
        }),
      ],
      blocks: [block(25, "li", "1. **Buy the big tent?** It costs 200.")],
    }),
    "follow-ups": section({
      key: "follow-ups",
      title: "Follow-ups",
      line: 28,
      start: 30,
      end: 31,
      items: [item({ number: 1, text: "Pay the baker on Friday.", first_line: 30, last_line: 31 })],
      blocks: [block(30, "li", "- Pay the baker on Friday.")],
    }),
    cost: section({
      key: "cost",
      title: "Cost",
      body: "12,000 tokens over 14 turns.",
      line: 32,
      start: 34,
      end: 35,
      rest: "12,000 tokens over 14 turns.",
      blocks: [block(34, "p", "12,000 tokens over 14 turns.")],
    }),
  },
};

const PLAIN: ShowcaseData = {
  ...REPORT,
  structured: false,
  lede: "",
  sections: {},
};

const PLAIN_BLOCKS = [block(1, "p", "It is done."), block(3, "li", "- Nothing is left.")];

const COMMENT: DocComment = {
  id: "1",
  line_no: 25,
  line_text: "1. **Buy the big tent?** It costs 200.",
  line_now: "1. **Buy the big tent?** It costs 200.",
  stale: false,
  body: "Yes, buy it.",
  author: "alex",
  created_at: null,
  resolved_at: null,
  resolution: null,
};

function gutter(fields: Partial<Gutter> = {}): Gutter {
  return {
    lines: LINES,
    prefix: "R",
    csrf: "a-token",
    person: "alex",
    cards: new Map(),
    orphans: [],
    resolved: [],
    marked: new Set(),
    open: 0,
    openForm: null,
    setOpenForm: () => undefined,
    error: "",
    busy: false,
    add: () => Promise.resolve(),
    remove: () => Promise.resolve(),
    ...fields,
  };
}

describe("a report as a showcase", () => {
  it("gives the highlights as cards", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    const cards = [...document.querySelectorAll("ul.a-hls > li.a-hl")];
    expect(cards.map((card) => card.id)).toEqual(["R7", "R8"]);
    expect(cards[0]?.querySelector(".a-hl-lead")?.textContent).toEqual("The tent is booked.");
    expect(cards[0]?.querySelector(".a-hl-text")?.textContent).toContain("It holds twenty.");
    expect(cards[1]?.querySelector(".a-hl-text")?.textContent).toContain("The food is ordered.");
  });

  it("gives the decisions as numbered cards", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    const cards = [...document.querySelectorAll("#decisions ol.a-decisions > li.a-decision")];
    expect(cards.map((card) => card.querySelector(".a-n")?.textContent)).toEqual(["1", "2"]);
    expect(cards.map((card) => card.querySelector(".a-q")?.textContent)).toEqual([
      "Buy the big tent?",
      "Hire a cook?",
    ]);
    expect(document.querySelector("#decisions .a-count")?.textContent).toEqual("2 decisions");
  });

  it("gives the outputs as cards, grouped by kind", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    const groups = [...document.querySelectorAll("#outputs .a-group")];
    expect(groups.map((group) => group.querySelector("h3")?.textContent)).toEqual([
      "document 1",
      "issue 1",
    ]);
    expect(document.querySelector("#outputs .a-count")?.textContent).toEqual("2 outputs");
    expect([...document.querySelectorAll("#outputs li.a-out")].map((card) => card.id)).toEqual([
      "R20",
      "R21",
    ]);
  });

  it("gives what was asked as a quote and what was done as prose", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    expect(document.querySelector("#asked blockquote.a-quote")?.textContent).toContain(
      "Plan the party for Saturday.",
    );
    expect(document.querySelector("#done .a-blocks")?.textContent).toContain(
      "Called the tent place and the baker.",
    );
  });

  it("gives the follow-ups as a checklist", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    const rows = [...document.querySelectorAll("#follow-ups ul.a-checklist > li")];
    expect(rows.length).toEqual(1);
    expect(rows[0]?.querySelector(".a-box")).not.toBeNull();
    expect(rows[0]?.textContent).toContain("Pay the baker on Friday.");
  });

  it("gives the cost as its blocks", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    expect(document.querySelector("#cost")?.textContent).toContain("12,000 tokens over 14 turns.");
  });

  it("shows no section whose body says nothing", () => {
    const quiet: ShowcaseData = {
      ...REPORT,
      sections: {
        ...REPORT.sections,
        "decisions for you": section({
          key: "decisions for you",
          title: "Decisions for you",
          line: 23,
          start: 25,
          end: 26,
          body: "None.",
          empty: true,
        }),
      },
    };

    render(<Showcase report={quiet} gutter={gutter()} />);

    expect(document.querySelector("#decisions")).toBeNull();
  });
});

describe("the comment gutter of a showcase", () => {
  it("gives the units in the order of the page, so a comment lands on a card", () => {
    expect(showcaseSpans(REPORT, [])).toEqual([
      { first: 5, last: 6 },
      { first: 7, last: 7 },
      { first: 8, last: 9 },
      { first: 23, last: 24 },
      { first: 25, last: 25 },
      { first: 26, last: 27 },
      { first: 18, last: 19 },
      { first: 20, last: 20 },
      { first: 21, last: 22 },
      { first: 10, last: 11 },
      { first: 12, last: 12 },
      { first: 14, last: 15 },
      { first: 16, last: 16 },
      { first: 28, last: 29 },
      { first: 30, last: 31 },
      { first: 32, last: 33 },
      { first: 34, last: 34 },
    ]);
  });

  it("gives the units of the blocks of a text that is no showcase", () => {
    expect(showcaseSpans(PLAIN, PLAIN_BLOCKS)).toEqual([
      { first: 1, last: 1 },
      { first: 3, last: 3 },
    ]);
    expect(showcaseSpans(null, PLAIN_BLOCKS)).toEqual([
      { first: 1, last: 1 },
      { first: 3, last: 3 },
    ]);
  });

  it("marks the card that holds a comment, and shows it under the card", () => {
    render(<Showcase report={REPORT} gutter={gutter({ cards: new Map([[25, [COMMENT]]]) })} />);

    const card = document.querySelector("#R25") as HTMLElement;
    expect(card.className).toContain("has-comments");
    expect(card.textContent).toContain("Yes, buy it.");
  });

  it("gives the heading of a section a + of its own", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    expect(document.querySelector("#decisions .a-head")?.id).toEqual("R23");
    expect(screen.getAllByTitle("Comment on the heading").length).toBeGreaterThan(0);
  });
});

describe("a text that is no showcase", () => {
  it("shows its blocks, each one a unit", () => {
    render(<Showcase report={PLAIN} blocks={PLAIN_BLOCKS} gutter={gutter()} />);

    expect(document.querySelector(".a-showcase")).toBeNull();
    const units = [...document.querySelectorAll(".a-blocks.d-body > .a-unit")];
    expect(units.map((unit) => unit.id)).toEqual(["R1", "R3"]);
  });

  it("shows the blocks when there is no report at all", () => {
    render(<Showcase report={null} blocks={PLAIN_BLOCKS} gutter={gutter()} />);

    expect(screen.getByText("It is done.")).toBeDefined();
  });
});

describe("the report of an assignment", () => {
  it("takes an answer on each decision", () => {
    render(<Showcase report={REPORT} gutter={gutter()} answers />);

    expect(screen.getAllByText(SEND).length).toEqual(2);
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it("offers the copy button of a document instead", () => {
    render(<Showcase report={REPORT} gutter={gutter()} />);

    expect(screen.getAllByRole("button", { name: "Copy" }).length).toEqual(2);
    expect(screen.queryByText(SEND)).toBeNull();
  });
});

describe("the rules the server reads", () => {
  it("knows a body that says nothing", () => {
    expect(isNone("")).toBe(true);
    expect(isNone("None.")).toBe(true);
    expect(isNone("No decisions.")).toBe(true);
    expect(isNone("Buy the tent.")).toBe(false);
  });

  it("gives the lines of the heading of a section", () => {
    expect(headSpan(section({ key: "cost", line: 32, start: 34 }))).toEqual({
      first: 32,
      last: 33,
    });
  });
});

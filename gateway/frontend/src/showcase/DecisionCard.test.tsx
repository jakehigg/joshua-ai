import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DecisionCard, { SEND, SENT } from "./DecisionCard";
import type { ReportItem } from "../assignments/data";
import type { DocComment } from "../comments/CommentCard";
import type { Gutter } from "../comments/Gutter";

const LINE = "- **Buy the big tent?** It costs 200.";

const ITEM: ReportItem = {
  number: 1,
  text: "**Buy the big tent?** It costs 200.",
  lead: "Buy the big tent?",
  body: "It costs 200.",
  line: 2,
  first_line: 3,
  last_line: 5,
  question: "Buy the big tent?",
  context: "It costs 200.",
  copy: "Document documents/alex/party.md, decision 1: Buy the big tent?",
};

const ANSWER: DocComment = {
  id: "7",
  line_no: 3,
  line_text: LINE,
  line_now: LINE,
  stale: false,
  body: "Yes, buy it.",
  author: "alex",
  created_at: "2026-11-09T12:00:00+00:00",
  resolved_at: null,
  resolution: null,
};

const DONE: DocComment = {
  ...ANSWER,
  id: "8",
  resolved_at: "2026-11-09T13:00:00+00:00",
  resolution: "The tent is booked.",
};

/** A gutter of the report, which holds the answers of the person. */
function gutter(fields: Partial<Gutter> = {}): Gutter {
  return {
    lines: ["## Decisions for you", "", LINE],
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

/** A clipboard that remembers what a button copied. */
function clipboard(): ReturnType<typeof vi.fn> {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  return writeText;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("one decision of a report", () => {
  it("is a numbered card of the gutter, on the lines it holds", () => {
    render(<DecisionCard item={ITEM} gutter={gutter()} />);

    const card = document.querySelector("li.a-decision") as HTMLElement;
    expect(card.className).toEqual("a-decision a-card-unit");
    expect(card.id).toEqual("R3");
    expect(card.querySelector(".a-n")?.textContent).toEqual("1");
    expect(card.querySelector(".a-q")?.textContent).toEqual("Buy the big tent?");
    expect(card.querySelector(".a-ctx")?.textContent).toContain("It costs 200.");
    // Every other line of the decision is an anchor, so a comment on one of
    // them scrolls to the card.
    expect(document.querySelector("#R4")?.className).toEqual("a-anchor");
    expect(document.querySelector("#R5")?.className).toEqual("a-anchor");
  });
});

describe("a decision of a document", () => {
  it("copies the text for Joshua, with no script of the page", () => {
    const writeText = clipboard();

    render(<DecisionCard item={ITEM} gutter={gutter()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith(ITEM.copy);
    expect(screen.getByRole("button", { name: "Copied" })).toBeDefined();
  });

  it("takes no answer, and the card takes a comment of its own", () => {
    render(<DecisionCard item={ITEM} gutter={gutter()} />);

    expect(screen.queryByRole("button", { name: SEND })).toBeNull();
    expect(screen.getByTitle("Comment on decision 1")).toBeDefined();
  });
});

describe("a decision of the report of an assignment", () => {
  it("takes an answer on the first line of the decision", () => {
    const wrote: unknown[] = [];
    const page = gutter({
      add: (lineNo: number, lineText: string, body: string) => {
        wrote.push([lineNo, lineText, body]);
        return Promise.resolve();
      },
    });

    render(<DecisionCard item={ITEM} gutter={page} answers />);
    fireEvent.change(screen.getByLabelText("Your answer to decision 1"), {
      target: { value: "Yes, buy it." },
    });
    fireEvent.click(screen.getByText(SEND));

    expect(wrote).toEqual([[3, LINE, "Yes, buy it."]]);
  });

  it("shows the answer of the person, and offers no form while it is open", () => {
    render(
      <DecisionCard item={ITEM} gutter={gutter({ cards: new Map([[3, [ANSWER]]]) })} answers />,
    );

    expect(document.querySelector("li.a-decision")?.className).toContain("answered");
    expect(document.querySelector(".a-answer-body")?.textContent).toEqual("Yes, buy it.");
    expect(screen.getByText(SENT)).toBeDefined();
    expect(screen.queryByText(SEND)).toBeNull();
    expect(screen.getByText("Delete")).toBeDefined();
  });

  it("shows the resolution of Joshua, and takes another answer", () => {
    render(<DecisionCard item={ITEM} gutter={gutter({ resolved: [DONE] })} answers />);

    expect(document.querySelector(".a-answer.resolved")?.textContent).toContain(
      "The tent is booked.",
    );
    expect(screen.getByText(SEND)).toBeDefined();
  });

  it("offers no form and no copy button with no token", () => {
    render(<DecisionCard item={ITEM} gutter={gutter({ csrf: null })} answers />);

    expect(screen.queryByText(SEND)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});

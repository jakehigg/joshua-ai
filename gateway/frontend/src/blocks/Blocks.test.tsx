import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Blocks, { BLOCK_NAMES, span, spans, what, type DocBlock } from "./Blocks";
import type { DocComment } from "../comments/CommentCard";
import type { Gutter } from "../comments/Gutter";

const BLOCKS: DocBlock[] = [
  { id: "L1", kind: "h", depth: 0, markdown: "# Party plan", first_line: 1, last_line: 2 },
  {
    id: "L3",
    kind: "p",
    depth: 0,
    markdown: "Food and music for twenty people.",
    first_line: 3,
    last_line: 4,
  },
  { id: "L5", kind: "li", depth: 0, markdown: "- Order the food", first_line: 5, last_line: 5 },
  { id: "L6", kind: "li", depth: 1, markdown: "- Book the music", first_line: 6, last_line: 6 },
  {
    id: "L7",
    kind: "code",
    depth: 0,
    markdown: "```sh\nmake test\n```",
    first_line: 7,
    last_line: 9,
  },
];

const COMMENT: DocComment = {
  id: "1",
  line_no: 3,
  line_text: "Food and music for twenty people.",
  line_now: "Food and music for twenty people.",
  stale: false,
  body: "Say how much food.",
  author: "alex",
  created_at: null,
  resolved_at: null,
  resolution: null,
};

/** A gutter that holds one card and writes nothing. */
function gutter(fields: Partial<Gutter> = {}): Gutter {
  return {
    lines: ["# Party plan", "", "Food and music for twenty people."],
    csrf: "a-token",
    person: "alex",
    cards: new Map(),
    orphans: [],
    resolved: [],
    marked: new Set(),
    open: 0,
    openLine: null,
    setOpenLine: () => undefined,
    error: "",
    busy: false,
    add: () => Promise.resolve(),
    remove: () => Promise.resolve(),
    ...fields,
  };
}

describe("the blocks of a document", () => {
  it("gives one unit a block, with its kind, its depth and its lines", () => {
    render(<Blocks blocks={BLOCKS} gutter={gutter()} />);

    const units = [...document.querySelectorAll(".a-blocks.d-body > .a-unit")];
    expect(units.map((unit) => unit.className)).toEqual([
      "a-unit a-block h d0",
      "a-unit a-block p d0",
      "a-unit a-block li d0",
      "a-unit a-block li d1",
      "a-unit a-block code d0",
    ]);
    expect(units.map((unit) => unit.id)).toEqual(["L1", "L3", "L5", "L6", "L7"]);
    // The lines after the first of a block are anchors of their own.
    expect(document.querySelector("#L2")?.className).toEqual("a-anchor");
    expect(document.querySelector("#L8")?.className).toEqual("a-anchor");
  });

  it("renders the Markdown of each block alone", () => {
    render(<Blocks blocks={BLOCKS} gutter={gutter()} />);

    expect(document.querySelector("#L1 h1")?.textContent).toEqual("Party plan");
    expect(document.querySelector("#L3 p")?.textContent).toEqual(
      "Food and music for twenty people.",
    );
    expect(document.querySelector("#L5 ul li")?.textContent).toEqual("Order the food");
    expect(document.querySelector("#L7 pre code")?.textContent).toEqual("make test\n");
    expect(document.querySelectorAll("article.j-prose").length).toEqual(BLOCKS.length);
  });

  it("names what a comment on a block is on", () => {
    render(<Blocks blocks={BLOCKS} gutter={gutter()} />);

    expect(screen.getByTitle("Comment on the heading at line 1")).toBeDefined();
    expect(screen.getByTitle("Comment on the paragraph at line 3")).toBeDefined();
    expect(screen.getByTitle("Comment on the list item at line 5")).toBeDefined();
    expect(screen.getByTitle("Comment on the code block at line 7")).toBeDefined();
  });

  it("shows the cards of the block the gutter placed them on", () => {
    render(<Blocks blocks={BLOCKS} gutter={gutter({ cards: new Map([[3, [COMMENT]]]) })} />);

    expect(document.querySelector("#L3.has-comments .a-comment-body")?.textContent).toEqual(
      "Say how much food.",
    );
    expect(document.querySelectorAll(".a-comment").length).toEqual(1);
  });

  it("has no + with no token", () => {
    render(<Blocks blocks={BLOCKS} gutter={gutter({ csrf: null })} />);

    expect(document.querySelector("button.v-plus")).toBeNull();
  });

  it("draws nothing for a document with no block", () => {
    render(<Blocks blocks={[]} gutter={gutter()} />);

    expect(document.querySelectorAll(".a-unit").length).toEqual(0);
    expect(document.querySelector(".a-blocks")).not.toBeNull();
  });
});

describe("a block as a unit", () => {
  it("carries its first line and its last line", () => {
    expect(span(BLOCKS[1] as DocBlock)).toEqual({ first: 3, last: 4 });
    expect(spans(BLOCKS)).toEqual([
      { first: 1, last: 2 },
      { first: 3, last: 4 },
      { first: 5, last: 5 },
      { first: 6, last: 6 },
      { first: 7, last: 9 },
    ]);
  });

  it("names every kind the server cuts, and a kind it does not know", () => {
    expect(Object.keys(BLOCK_NAMES)).toEqual(["p", "h", "li", "quote", "code", "table"]);
    expect(what({ ...(BLOCKS[0] as DocBlock), kind: "hr" })).toEqual("the block at line 1");
  });
});

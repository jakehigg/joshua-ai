import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DocComment } from "./CommentCard";
import { Orphans, Resolved, SourceLines, Unit, useGutter, type Span } from "./Gutter";

const LINES = ["# Party plan", "", "Food and music.", "", "- Order the food", "- Book the music"];
const PREAMBLE = ["---", "title: Party plan", "---"];
const UNITS: Span[] = [
  { first: 0, last: 0 },
  { first: 1, last: 2 },
  { first: 3, last: 4 },
  { first: 5, last: 5 },
  { first: 6, last: 6 },
];
const URL = "/documents/alex/party";

const COMMENT: DocComment = {
  id: "1",
  line_no: 3,
  line_text: "Food and music.",
  line_now: "Food and music.",
  stale: false,
  body: "Say how much food.",
  author: "alex",
  created_at: "2026-11-09T12:00:00+00:00",
  resolved_at: null,
  resolution: null,
};

function one(fields: Partial<DocComment>): DocComment {
  return { ...COMMENT, ...fields };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The write of the gutter: what it posted, and what the viewer answers. */
function stubWrite(answer: Response = json({ ok: true, anchor: "L3", comments: [] })) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    sent.push({ path, body: JSON.parse(String(init?.body)) });
    return Promise.resolve(answer.clone());
  });
  return sent;
}

/**
 * A page of units, as a document page builds it: the head of line 0, one unit
 * a block, the orphans, the resolved comments, and the Source fold.
 */
function Harness({
  comments,
  csrf = "a-token",
  person = "alex",
}: {
  comments: DocComment[];
  csrf?: string | null;
  person?: string;
}) {
  const [held, setHeld] = useState(comments);
  const gutter = useGutter({
    url: URL,
    lines: LINES,
    comments: held,
    csrf,
    person,
    units: UNITS,
    onComments: setHeld,
  });
  return (
    <>
      <p>{`${gutter.open} open`}</p>
      {UNITS.map((unit) => (
        <Unit
          key={unit.first}
          gutter={gutter}
          unit={unit}
          className={`a-block l${unit.first}`}
          what={unit.first === 0 ? "the document" : `the block at line ${unit.first}`}
        >
          <p>{`block ${unit.first}`}</p>
        </Unit>
      ))}
      <Orphans gutter={gutter} />
      <Resolved gutter={gutter} />
      <SourceLines gutter={gutter} preamble={PREAMBLE} />
    </>
  );
}

function page(props: { comments: DocComment[]; csrf?: string | null; person?: string }) {
  return render(<Harness {...props} />);
}

/** The cards of the unit whose first line is `first`. */
function cards(first: number): string[] {
  const unit = document.querySelector(`.a-unit.l${first}`);
  return [...(unit?.querySelectorAll(".a-comment-body") ?? [])].map(
    (item) => item.textContent ?? "",
  );
}

function plus(label: string): HTMLButtonElement {
  return screen.getByTitle(label) as HTMLButtonElement;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("where a comment shows", () => {
  it("puts a comment under the first unit that holds its line", () => {
    page({ comments: [one({ id: "1", line_no: 3 }), one({ id: "2", line_no: 4, body: "And?" })] });

    expect(cards(3)).toEqual(["Say how much food.", "And?"]);
    expect(cards(1)).toEqual([]);
    expect(screen.getByText("2 open")).toBeDefined();
    expect(document.querySelector(".a-unit.l3.has-comments")).not.toBeNull();
    expect(document.querySelector(".a-unit.l1.has-comments")).toBeNull();
  });

  it("quotes the line when it is not the first line of the unit", () => {
    page({ comments: [one({ line_no: 4, line_text: "" })] });

    expect(cards(3)).toEqual(["Say how much food."]);
    expect(screen.getByText("Line 4:")).toBeDefined();
  });

  it("puts a comment on line 0 under the head", () => {
    page({ comments: [one({ line_no: 0, line_text: "title: Party plan" })] });

    expect(cards(0)).toEqual(["Say how much food."]);
  });

  it("puts a comment that no unit holds under one label", () => {
    page({ comments: [one({ line_no: 40, stale: true })] });

    expect(cards(3)).toEqual([]);
    expect(screen.getByText("On lines that are gone")).toBeDefined();
    expect(document.querySelector(".a-orphans .a-comment-body")?.textContent).toEqual(
      "Say how much food.",
    );
  });

  it("folds the resolved comments, and keeps them out of the units", () => {
    page({
      comments: [one({ resolved_at: "2026-11-09T14:00:00+00:00", resolution: "Done." })],
    });

    expect(cards(3)).toEqual([]);
    expect(screen.getByText("0 open")).toBeDefined();
    expect(document.querySelector("details.a-resolved summary")?.textContent).toEqual("1 resolved");
    expect(document.querySelector(".a-resolved-list .a-comment.resolved")).not.toBeNull();
  });

  it("anchors each line of a unit after its first", () => {
    page({ comments: [] });

    expect(document.querySelector(".a-unit.l3")?.id).toEqual("L3");
    expect(document.querySelector("#L4")?.className).toEqual("a-anchor");
  });
});

describe("the form of a comment", () => {
  it("has a + for each unit and each line of the source", () => {
    page({ comments: [] });

    expect(plus("Comment on the document")).toBeDefined();
    expect(plus("Comment on the block at line 3")).toBeDefined();
    expect(screen.getAllByTitle("Comment on line 5").length).toEqual(1);
    expect(document.querySelectorAll("ol.a-lines button.v-plus").length).toEqual(LINES.length);
  });

  it("posts the line, the line text and the body, then shows the comments", async () => {
    const sent = stubWrite(json({ ok: true, anchor: "L3", comments: [one({ body: "Stored." })] }));

    page({ comments: [] });
    fireEvent.click(plus("Comment on the block at line 3"));
    fireEvent.change(screen.getByLabelText("Your comment on the block at line 3"), {
      target: { value: "Say how much food." },
    });
    fireEvent.click(screen.getByText("Comment"));

    expect(await screen.findByText("Stored.")).toBeDefined();
    expect(sent).toEqual([
      {
        path: "/api/v1/documents/alex/party/comments",
        body: {
          csrf: "a-token",
          line_no: 3,
          line_text: "Food and music.",
          body: "Say how much food.",
        },
      },
    ]);
    // The form closed, so the "+" is back.
    expect(plus("Comment on the block at line 3")).toBeDefined();
  });

  it("posts the line of the source fold that the person picked", async () => {
    const sent = stubWrite();

    page({ comments: [] });
    const lines = [...document.querySelectorAll("ol.a-lines button.v-plus")];
    fireEvent.click(lines[4] as HTMLButtonElement);
    fireEvent.change(screen.getByLabelText("Your comment on line 5"), {
      target: { value: "Ask the baker." },
    });
    fireEvent.click(screen.getByText("Comment"));

    await screen.findByTitle("Comment on line 5");
    expect(sent).toEqual([
      {
        path: "/api/v1/documents/alex/party/comments",
        body: {
          csrf: "a-token",
          line_no: 5,
          line_text: "- Order the food",
          body: "Ask the baker.",
        },
      },
    ]);
  });

  it("opens one form at a time", () => {
    page({ comments: [] });

    fireEvent.click(plus("Comment on the block at line 3"));
    expect(document.querySelectorAll("form.a-comment-form").length).toEqual(1);
    fireEvent.click(plus("Comment on the block at line 1"));
    expect(document.querySelectorAll("form.a-comment-form").length).toEqual(1);
    expect(screen.getByLabelText("Your comment on the block at line 1")).toBeDefined();
  });

  it("closes the form on Cancel, and posts nothing", () => {
    const sent = stubWrite();

    page({ comments: [] });
    fireEvent.click(plus("Comment on the block at line 3"));
    fireEvent.click(screen.getByText("Cancel"));

    expect(document.querySelector("form.a-comment-form")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("says what the viewer said on a refusal, and keeps the form", async () => {
    stubWrite(json({ error: "The comment is empty." }, 400));

    page({ comments: [] });
    fireEvent.click(plus("Comment on the block at line 3"));
    fireEvent.change(screen.getByLabelText("Your comment on the block at line 3"), {
      target: { value: "  " },
    });
    fireEvent.click(screen.getByText("Comment"));

    const line = await screen.findByText("The comment is empty.");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("form.a-comment-form")).not.toBeNull();
  });

  it("marks the lines of the source that hold a comment", () => {
    page({ comments: [one({ line_no: 3 })] });

    expect(document.querySelector("li.a-line.has-comments")?.id).toEqual("S3");
    expect(document.querySelectorAll("li.a-line.a-pre").length).toEqual(PREAMBLE.length);
    expect(document.querySelectorAll("li.a-line.a-pre .a-ln")[0]?.textContent).toEqual("");
    expect(document.querySelectorAll("ol.a-lines > li").length).toEqual(
      PREAMBLE.length + LINES.length,
    );
  });
});

describe("a read-only gutter", () => {
  it("shows the cards and no +", () => {
    page({ comments: [one({ line_no: 3 })], csrf: null });

    expect(cards(3)).toEqual(["Say how much food."]);
    expect(document.querySelector("button.v-plus")).toBeNull();
    expect(screen.queryByText("Delete")).toBeNull();
  });
});

describe("the delete of a comment", () => {
  it("posts the token to the address of the comment, and shows what is left", async () => {
    const sent = stubWrite(json({ ok: true, comments: [] }));

    page({ comments: [one({ id: "7", line_no: 3 })] });
    fireEvent.click(screen.getByText("Delete"));

    await screen.findByText("0 open");
    expect(sent).toEqual([
      { path: "/api/v1/documents/alex/party/comments/7/delete", body: { csrf: "a-token" } },
    ]);
    expect(cards(3)).toEqual([]);
  });

  it("gives no Delete on a comment of somebody else", () => {
    page({ comments: [one({ line_no: 3, author: "mia" })] });

    expect(cards(3)).toEqual(["Say how much food."]);
    expect(screen.queryByText("Delete")).toBeNull();
  });
});

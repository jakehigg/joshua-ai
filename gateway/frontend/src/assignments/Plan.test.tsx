import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Plan, { Progress, task } from "./Plan";
import type { PlanData } from "./data";
import type { DocBlock } from "../blocks/Blocks";
import type { DocComment } from "../comments/CommentCard";

const URL = "/assignments/7";

const LINES = ["## Steps", "", "- [x] read the pages", "- [ ] fix the links"];

const BLOCKS: DocBlock[] = [
  { id: "P1", kind: "h", depth: 0, markdown: "## Steps", first_line: 1, last_line: 1 },
  { id: "P3", kind: "li", depth: 0, markdown: "- [x] read the pages", first_line: 3, last_line: 3 },
  { id: "P4", kind: "li", depth: 0, markdown: "- [ ] fix the links", first_line: 4, last_line: 4 },
];

const PLAN: PlanData = {
  markdown: LINES.join("\n"),
  lines: LINES,
  blocks: BLOCKS,
  updated: "5m ago",
  progress: { done: 1, total: 2, skipped: 0, open: 1, next: "fix the links", percent: 50 },
};

const COMMENT: DocComment = {
  id: "1",
  line_no: 4,
  line_text: "- [ ] fix the links",
  line_now: "- [ ] fix the links",
  stale: false,
  body: "Start with the index.",
  author: "alex",
  created_at: "2026-11-09T12:00:00+00:00",
  resolved_at: null,
  resolution: null,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** What the page posts, and what the viewer answers. */
function stubWrite(answer: Response = json({ ok: true, anchor: "P4" })) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    sent.push({ path, body: JSON.parse(String(init?.body)) });
    return Promise.resolve(answer.clone());
  });
  return sent;
}

function page({ plan = PLAN, comments = [] as DocComment[], csrf = "a-token" as string | null }) {
  return render(
    <Plan
      url={URL}
      plan={plan}
      comments={comments}
      csrf={csrf}
      person="alex"
      onComments={() => undefined}
      reload={() => undefined}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a task box", () => {
  it("reads the box of a list item, and nothing of another block", () => {
    expect(task(BLOCKS[1] as DocBlock)).toEqual({
      done: true,
      skipped: false,
      text: "read the pages",
    });
    expect(task(BLOCKS[2] as DocBlock)).toEqual({
      done: false,
      skipped: false,
      text: "fix the links",
    });
    expect(task(BLOCKS[0] as DocBlock)).toBeNull();
    expect(task({ ...(BLOCKS[1] as DocBlock), markdown: "- read the pages" })).toBeNull();
  });

  it("reads the box of a step that the worker marked skipped", () => {
    const skipped = { ...(BLOCKS[2] as DocBlock), markdown: "- [-] fix the links — skipped: no" };
    expect(task(skipped)).toEqual({
      done: false,
      skipped: true,
      text: "fix the links — skipped: no",
    });
  });
});

describe("the progress of the plan", () => {
  it("gives the steps that are done, the bar, and the next step", () => {
    render(<Progress plan={PLAN} />);

    expect(document.querySelector(".a-steps")?.textContent).toEqual("1 of 2 steps");
    expect(document.querySelector("svg.a-meter .fill")?.getAttribute("width")).toEqual("50");
    expect(document.querySelector(".a-now-line")?.textContent).toEqual("Nextfix the links");
  });

  it("gives nothing when there is no box and no next step", () => {
    render(
      <Progress
        plan={{ ...PLAN, progress: { done: 0, total: 0, skipped: 0, open: 0, next: "", percent: 0 } }}
      />,
    );

    expect(document.querySelector(".a-now")).toBeNull();
  });

  it("names the steps that the worker marked skipped", () => {
    const progress = { done: 1, total: 3, skipped: 1, open: 1, next: "fix the links", percent: 66.7 };
    render(<Progress plan={{ ...PLAN, progress }} />);

    expect(document.querySelector(".a-steps")?.textContent).toEqual("1 of 3 steps, 1 skipped");
  });

  it("says that a run which ended has open steps, in place of the bar", () => {
    const progress = { done: 5, total: 7, skipped: 0, open: 2, next: "write the report", percent: 71 };
    render(<Progress plan={{ ...PLAN, progress }} ended />);

    expect(document.querySelector(".a-open-steps")?.textContent).toEqual("ended, 2 steps open");
    expect(document.querySelector("svg.a-meter")).toBeNull();
    expect(document.querySelector(".a-now-line")).toBeNull();
  });

  it("keeps the bar of a run that ended with every step settled", () => {
    const progress = { done: 6, total: 7, skipped: 1, open: 0, next: "", percent: 100 };
    render(<Progress plan={{ ...PLAN, progress }} ended />);

    expect(document.querySelector(".a-open-steps")).toBeNull();
    expect(document.querySelector("svg.a-meter .fill")?.getAttribute("width")).toEqual("100");
  });
});

describe("the plan of an assignment", () => {
  it("draws a task box as a checklist and keeps the other blocks", () => {
    page({});

    expect(document.querySelector("#P1 h2")?.textContent).toEqual("Steps");
    const boxes = [...document.querySelectorAll("ul.a-checklist li")];
    expect(boxes.map((item) => item.textContent)).toEqual(["read the pages", "fix the links"]);
    expect(boxes[0]?.className).toEqual("done");
    expect(boxes[1]?.className).toEqual("");
    expect(screen.getByLabelText("done")).toBeDefined();
    expect(screen.getByLabelText("to do")).toBeDefined();
    expect(screen.getByText("updated 5m ago")).toBeDefined();
  });

  it("gives each block the + of its line and shows its comments", () => {
    page({ comments: [COMMENT] });

    expect(screen.getByTitle("Comment on the list item at line 4")).toBeDefined();
    expect(document.querySelector("#P4.has-comments .a-comment-body")?.textContent).toEqual(
      "Start with the index.",
    );
  });

  it("posts the comment with the line and the target of the plan", async () => {
    const sent = stubWrite();

    page({});
    fireEvent.click(screen.getByTitle("Comment on the list item at line 4"));
    fireEvent.change(screen.getByLabelText("Your comment on the list item at line 4"), {
      target: { value: "Start with the index." },
    });
    fireEvent.click(screen.getByText("Comment"));

    await screen.findByTitle("Comment on the list item at line 4");
    expect(sent).toEqual([
      {
        path: "/api/v1/assignments/7/comments",
        body: {
          target: "plan",
          csrf: "a-token",
          line_no: 4,
          line_text: "- [ ] fix the links",
          body: "Start with the index.",
        },
      },
    ]);
  });

  it("draws the box of a skipped step with its own mark", () => {
    const blocks = [
      ...BLOCKS.slice(0, 2),
      { ...(BLOCKS[2] as DocBlock), markdown: "- [-] fix the links — skipped: the host is down" },
    ];
    page({ plan: { ...PLAN, blocks } });

    expect(document.querySelector("ul.a-checklist li.skipped")?.textContent).toEqual(
      "fix the links — skipped: the host is down",
    );
    expect(screen.getByLabelText("skipped")).toBeDefined();
  });

  it("says there is no plan yet when the manager wrote none", () => {
    page({
      plan: {
        markdown: "",
        lines: [],
        blocks: [],
        updated: "",
        progress: { done: 0, total: 0, skipped: 0, open: 0, next: "", percent: 0 },
      },
    });

    expect(screen.getByText("No plan yet.")).toBeDefined();
    expect(document.querySelector(".a-blocks")).toBeNull();
  });
});

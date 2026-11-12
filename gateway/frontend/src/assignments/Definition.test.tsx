import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Definition, { span, unitClass } from "./Definition";
import type { DefinitionData, DefinitionUnit } from "./data";
import type { DocComment } from "../comments/CommentCard";

const URL = "/assignments/7";

const LINES = [
  "title: Clean up the docs",
  "goal: |",
  "  Read every page of the viewer.",
  "",
  "  - Fix the links",
  "tags: [docs, viewer]",
];

const UNITS: DefinitionUnit[] = [
  {
    id: "L1",
    first_line: 1,
    last_line: 1,
    kind: "prop",
    label: "title",
    value: { kind: "text", text: "Clean up the docs" },
    markdown: "",
    depth: 0,
    what: "the title",
  },
  {
    id: "L6",
    first_line: 6,
    last_line: 6,
    kind: "prop",
    label: "tags",
    value: { kind: "tags", tags: ["docs", "viewer"] },
    markdown: "",
    depth: 0,
    what: "the tags",
  },
  {
    id: "L2",
    first_line: 2,
    last_line: 2,
    kind: "goal",
    label: "goal",
    value: null,
    markdown: "",
    depth: 0,
    what: "the goal",
  },
  {
    id: "L3",
    first_line: 3,
    last_line: 4,
    kind: "p",
    label: "",
    value: null,
    markdown: "Read every page of the viewer.",
    depth: 0,
    what: "the paragraph at line 3",
  },
  {
    id: "L5",
    first_line: 5,
    last_line: 5,
    kind: "li",
    label: "",
    value: null,
    markdown: "- Fix the links",
    depth: 0,
    what: "the list item at line 5",
  },
];

const DEFINITION: DefinitionData = { units: UNITS, lines: LINES, goal_offset: 2 };

const COMMENT: DocComment = {
  id: "1",
  line_no: 3,
  line_text: "  Read every page of the viewer.",
  line_now: "  Read every page of the viewer.",
  stale: false,
  body: "Name the pages.",
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
function stubWrite(answer: Response = json({ ok: true, anchor: "L3" })) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    sent.push({ path, body: JSON.parse(String(init?.body)) });
    return Promise.resolve(answer.clone());
  });
  return sent;
}

function page({
  definition = DEFINITION,
  comments = [] as DocComment[],
  csrf = "a-token" as string | null,
  open = false,
  reload = () => undefined,
}) {
  return render(
    <Definition
      url={URL}
      definition={definition}
      comments={comments}
      csrf={csrf}
      person="alex"
      onComments={() => undefined}
      reload={reload}
      open={open}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the parts of one unit", () => {
  it("reads the lines of a unit, and names its class", () => {
    expect(span(UNITS[0] as DefinitionUnit)).toEqual({ first: 1, last: 1 });
    expect(unitClass(UNITS[0] as DefinitionUnit)).toEqual("a-prop");
    expect(unitClass(UNITS[2] as DefinitionUnit)).toEqual("a-goal-head");
    expect(unitClass(UNITS[4] as DefinitionUnit)).toEqual("a-block li d0");
  });
});

describe("the definition as a document", () => {
  it("gives each field a property row and the goal its blocks", () => {
    page({ open: true });

    const props = [...document.querySelectorAll(".a-props .a-prop-key")];
    expect(props.map((item) => item.textContent)).toEqual(["title", "tags"]);
    expect(document.querySelector(".a-props .a-prop-text")?.textContent).toEqual(
      "Clean up the docs",
    );
    expect([...document.querySelectorAll(".a-tag")].map((item) => item.textContent)).toEqual([
      "docs",
      "viewer",
    ]);
    expect(document.querySelector(".a-goal .a-goal-head .a-prop-key")?.textContent).toEqual(
      "goal",
    );
    expect(document.querySelector("#L3 p")?.textContent).toEqual(
      "Read every page of the viewer.",
    );
    expect(document.querySelector("#L5 ul li")?.textContent).toEqual("Fix the links");
  });

  it("gives the + of each unit and of each line of the source", () => {
    page({ open: true });

    expect(screen.getByTitle("Comment on the title")).toBeDefined();
    expect(screen.getByTitle("Comment on the paragraph at line 3")).toBeDefined();
    expect(screen.getByText("6 lines")).toBeDefined();
    expect(document.querySelectorAll("ol.a-lines button.v-plus").length).toEqual(LINES.length);
    expect(document.querySelector("ol.a-lines #S1 .a-code")?.textContent).toEqual(
      "title: Clean up the docs",
    );
  });

  it("shows a comment under the unit that holds its line, and opens the fold", () => {
    page({ comments: [COMMENT] });

    expect(screen.getByText("Name the pages.")).toBeDefined();
    expect(document.querySelector("#L3.has-comments")).not.toBeNull();
    expect(document.querySelector("details.a-def")?.hasAttribute("open")).toBe(true);
    expect(document.querySelector(".a-comments")?.textContent).toEqual("1 comment for Joshua");
  });

  it("posts the comment with the line and the target of the definition", async () => {
    const sent = stubWrite();

    page({ open: true });
    fireEvent.click(screen.getByTitle("Comment on the paragraph at line 3"));
    fireEvent.change(screen.getByLabelText("Your comment on the paragraph at line 3"), {
      target: { value: "Name the pages." },
    });
    fireEvent.click(screen.getByText("Comment"));

    await screen.findByTitle("Comment on the paragraph at line 3");
    expect(sent).toEqual([
      {
        path: "/api/v1/assignments/7/comments",
        body: {
          target: "definition",
          csrf: "a-token",
          line_no: 3,
          line_text: "  Read every page of the viewer.",
          body: "Name the pages.",
        },
      },
    ]);
  });

  it("reads the page again, because the manager sends no comment back", async () => {
    stubWrite();
    const reload = vi.fn();

    page({ open: true, reload });
    fireEvent.click(screen.getByTitle("Comment on the title"));
    fireEvent.change(screen.getByLabelText("Your comment on the title"), {
      target: { value: "Shorter." },
    });
    fireEvent.click(screen.getByText("Comment"));

    await screen.findByTitle("Comment on the title");
    expect(reload).toHaveBeenCalled();
  });

  it("says what the viewer said when the comment is refused", async () => {
    stubWrite(json({ error: "The comment is empty." }, 400));

    page({ open: true });
    fireEvent.click(screen.getByTitle("Comment on the title"));
    fireEvent.change(screen.getByLabelText("Your comment on the title"), {
      target: { value: " " },
    });
    fireEvent.click(screen.getByText("Comment"));

    expect(await screen.findAllByText("The comment is empty.")).toBeDefined();
  });
});

describe("a definition with no text of the manager", () => {
  it("gives the spec as source and takes no comment", () => {
    page({
      definition: { units: [], lines: ["title: Clean up the docs"], goal_offset: 0 },
      csrf: null,
      open: true,
    });

    expect(document.querySelector(".a-doc pre code")?.textContent).toEqual(
      "title: Clean up the docs",
    );
    expect(document.querySelector("button.v-plus")).toBeNull();
  });

  it("says the definition is empty when there is no line at all", () => {
    page({ definition: { units: [], lines: [], goal_offset: 0 }, csrf: null, open: true });

    expect(screen.getByText("The definition is empty.")).toBeDefined();
  });
});

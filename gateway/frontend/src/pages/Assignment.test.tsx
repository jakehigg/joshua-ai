import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Assignment from "./Assignment";
import { NOT_CONFIGURED } from "./Assignments";
import type { AssignmentData } from "../assignments/data";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "assignments", label: "Assignments", path: "/assignments" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const DEFINITION_LINES = ["title: Clean up the docs", "goal: |", "  Read every page."];
const PLAN_LINES = ["- [x] read the pages", "- [ ] fix the links"];

const DATA: AssignmentData = {
  configured: true,
  available: true,
  not_found: false,
  item: {
    id: 7,
    title: "Clean up the docs",
    state: "in_progress",
    status: "running",
    status_label: "running",
    when: "started 2h ago",
    model: "sonnet",
    usage: { input: 10, output: 20, cache_read: 0, cache_write: 0, total: 12000 },
    cost: "12k tokens",
    cost_title: "12,000 tokens: 10 in, 20 out",
    duration: "1h 20m",
    turns: 14,
    started_at: "2026-11-09T13:00:00+00:00",
    finished_at: null,
    error: "",
    open_comments: 1,
    start_phrase: "",
    draft_line: "",
    notice: null,
    checkpoints: [{ at: "2026-11-09T14:00:00+00:00", when: "1h ago", note: "Read the pages." }],
    outputs: [],
    open_request: null,
    lineage: [
      {
        id: 7,
        url: "/assignments/7",
        title: "Clean up the docs",
        status: "running",
        status_label: "running",
        when: "1h ago",
        current: true,
      },
    ],
    run_number: { run: 2, root: 5 },
    saved_ref: { id: 3, name: "Weekly cleanup" },
  },
  definition: {
    units: [
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
        id: "L3",
        first_line: 3,
        last_line: 3,
        kind: "p",
        label: "",
        value: null,
        markdown: "Read every page.",
        depth: 0,
        what: "the paragraph at line 3",
      },
    ],
    lines: DEFINITION_LINES,
    goal_offset: 2,
  },
  plan: {
    markdown: PLAN_LINES.join("\n"),
    lines: PLAN_LINES,
    blocks: [
      {
        id: "P1",
        kind: "li",
        depth: 0,
        markdown: "- [x] read the pages",
        first_line: 1,
        last_line: 1,
      },
      {
        id: "P2",
        kind: "li",
        depth: 0,
        markdown: "- [ ] fix the links",
        first_line: 2,
        last_line: 2,
      },
    ],
    updated: "5m ago",
    progress: { done: 1, total: 2, next: "fix the links", percent: 50 },
  },
  report: null,
  comments: {
    definition: [
      {
        id: "1",
        line_no: 3,
        line_text: "  Read every page.",
        line_now: "  Read every page.",
        stale: false,
        body: "Name the pages.",
        author: "alex",
        created_at: "2026-11-09T12:00:00+00:00",
        resolved_at: null,
        resolution: null,
      },
    ],
    plan: [],
    report: [],
  },
  csrf: "a-token",
  refresh_seconds: 20,
  timezone: "UTC",
  now: "2026-11-09T15:00:00+00:00",
};

const DRAFT: AssignmentData = {
  ...DATA,
  item: {
    ...(DATA.item as NonNullable<AssignmentData["item"]>),
    id: 9,
    state: "draft",
    status: "draft",
    status_label: "draft",
    when: "created 5m ago",
    cost: "",
    duration: "",
    turns: null,
    started_at: null,
    open_comments: 0,
    start_phrase: 'Say "start assignment 9".',
    draft_line: "Tell Joshua to start it, or leave comments first.",
    run_number: null,
    saved_ref: null,
    lineage: [],
  },
  comments: { definition: [], plan: [], report: [] },
  refresh_seconds: null,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The assignment and the session, with one answer for every read. */
function stubOne(answer: Response = json(DATA)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    if (path.startsWith("/api/v1/assignments/")) {
      return Promise.resolve(answer.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

/** The page once the assignment is there: its title is the only h1. */
function loaded(): Promise<HTMLElement> {
  return screen.findByRole("heading", { level: 1, name: "Clean up the docs" });
}

function page(address = "/assignments/7") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/assignments/:id" element={<Assignment />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the head of an assignment", () => {
  it("gives the status, the marks, the title and the numbers", async () => {
    stubOne();

    page();

    expect(await loaded()).toBeDefined();
    const hero = document.querySelector("header.a-hero") as HTMLElement;
    expect(hero.querySelector(".j-badge.accent")?.textContent).toEqual("running");
    expect(hero.querySelector("a.a-saved")?.getAttribute("href")).toEqual("/assignments/saved/3");
    expect(hero.querySelector("a.a-run")?.textContent).toEqual("run 2 of assignment 5");
    expect(hero.querySelector(".a-comments")?.textContent).toEqual("1 comment for Joshua");
    expect(hero.querySelector(".j-meta")?.textContent).toEqual("started 2h ago");
    expect([...hero.querySelectorAll(".j-stat b")].map((item) => item.textContent)).toEqual([
      "12k tokens",
      "1h 20m",
      "14",
      "sonnet",
    ]);
    expect(document.title).toEqual("Clean up the docs");
  });

  it("names the assignments in the crumbs", async () => {
    stubOne();

    page();

    await loaded();
    expect(document.querySelector("nav.j-crumbs a")?.getAttribute("href")).toEqual("/assignments");
  });
});

describe("the body of an assignment", () => {
  it("gives the definition, the plan, the progress and the report", async () => {
    const asked = stubOne();

    page();

    await loaded();
    expect(asked).toContain("/api/v1/assignments/7");
    expect(document.querySelector("#definition .a-prop-key")?.textContent).toEqual("title");
    expect(screen.getByText("Name the pages.")).toBeDefined();
    expect(document.querySelector("#plan .a-steps")?.textContent).toEqual("1 of 2 steps");
    expect(
      [...document.querySelectorAll("#plan ul.a-checklist li")].map((item) => item.textContent),
    ).toEqual(["read the pages", "fix the links"]);
    expect(document.querySelector("#progress .a-text")?.textContent).toEqual("Read the pages.");
    expect(screen.getByText("No report yet.")).toBeDefined();
  });

  it("gives the report as blocks when the assignment wrote one", async () => {
    stubOne(
      json({
        ...DATA,
        report: {
          markdown: "It is done.",
          lines: ["It is done."],
          blocks: [
            {
              id: "R1",
              kind: "p",
              depth: 0,
              markdown: "It is done.",
              first_line: 1,
              last_line: 1,
            },
          ],
          updated: "2m ago",
          title: "Clean up the docs",
          lede: "",
          structured: false,
          sections: {},
        },
      }),
    );

    page();

    expect(await screen.findByText("It is done.")).toBeDefined();
    expect(document.querySelector("#report .j-meta")?.textContent).toEqual("updated 2m ago");
    expect(screen.getByTitle("Comment on the paragraph at line 1")).toBeDefined();
  });

  it("shows a draft with its definition open, and nothing of a run", async () => {
    stubOne(json(DRAFT));

    page("/assignments/9");

    expect(await screen.findByText('Say "start assignment 9".')).toBeDefined();
    expect(document.querySelector("details.a-def")?.hasAttribute("open")).toBe(true);
    expect(
      screen.getByText("Tell Joshua to start it, or leave comments first."),
    ).toBeDefined();
    expect(document.querySelector("#plan")).toBeNull();
    expect(document.querySelector("#progress")).toBeNull();
    expect(document.querySelector("#report")).toBeNull();
  });

  it("gives the error of a run that failed", async () => {
    stubOne(
      json({
        ...DATA,
        item: { ...DATA.item, state: "completed", status: "failed", error: "the tool refused" },
      }),
    );

    page();

    expect(await screen.findByText("the tool refused")).toBeDefined();
    expect(document.querySelector("pre.a-error")).not.toBeNull();
  });
});

describe("an assignment that is in progress", () => {
  it("reads itself again every refresh_seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const asked = stubOne();

    page();

    await loaded();
    const first = asked.filter((path) => path === "/api/v1/assignments/7").length;
    await vi.advanceTimersByTimeAsync(20_000);
    await waitFor(() => {
      expect(asked.filter((path) => path === "/api/v1/assignments/7").length).toEqual(first + 1);
    });
  });

  it("stops when the assignment is finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const asked = stubOne(
      json({
        ...DATA,
        item: { ...DATA.item, state: "completed", status: "done", status_label: "done" },
        refresh_seconds: null,
      }),
    );

    page();

    await loaded();
    const first = asked.filter((path) => path === "/api/v1/assignments/7").length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(asked.filter((path) => path === "/api/v1/assignments/7").length).toEqual(first);
  });
});

describe("an assignment that is not shown", () => {
  it("says the manager is not configured, and shows no assignment", async () => {
    stubOne(
      json({
        ...DATA,
        configured: false,
        available: false,
        item: null,
        definition: null,
        plan: null,
        csrf: null,
        refresh_seconds: null,
      }),
    );

    page();

    expect(await screen.findByText(NOT_CONFIGURED)).toBeDefined();
    expect(document.querySelector("header.a-hero")).toBeNull();
  });

  it("says what the viewer said about a row of another person", async () => {
    stubOne(json({ error: "not found" }, 404));

    page("/assignments/99");

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("header.a-hero")).toBeNull();
  });
});

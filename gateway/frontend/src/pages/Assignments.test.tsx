import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Assignments, { NOT_CONFIGURED, NO_ANSWER } from "./Assignments";
import type { AssignmentList, AssignmentRow } from "../assignments/data";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "assignments", label: "Assignments", path: "/assignments" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const ROW: AssignmentRow = {
  id: 7,
  url: "/assignments/7",
  title: "Clean up the docs",
  state: "in_progress",
  status: "running",
  status_label: "running",
  when: "started 2h ago",
  duration: "1h 20m",
  cost: "12k tokens",
  turns: 14,
  summary_line: "The docs of the viewer.",
  asks: false,
  start_phrase: "",
};

const DRAFT: AssignmentRow = {
  ...ROW,
  id: 9,
  url: "/assignments/9",
  title: "Book the hall",
  state: "draft",
  status: "draft",
  status_label: "draft",
  when: "created 5m ago",
  duration: "",
  cost: "",
  turns: null,
  summary_line: "",
  start_phrase: 'Say "start assignment 9".',
};

const LIST: AssignmentList = {
  configured: true,
  available: true,
  state: "in_progress",
  states: [
    { key: "draft", label: "Drafts" },
    { key: "in_progress", label: "In progress" },
    { key: "completed", label: "Completed" },
  ],
  items: [ROW],
  empty: "No assignment is in progress.",
  timezone: "UTC",
  now: "2026-11-09T15:00:00+00:00",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The list and the session. The answer is the same for every state. */
function stubList(list: Response = json(LIST)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    if (path.startsWith("/api/v1/assignments")) {
      return Promise.resolve(list.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(query = "") {
  return render(
    <MemoryRouter initialEntries={[`/assignments${query}`]}>
      <Routes>
        <Route path="/assignments" element={<Assignments />} />
      </Routes>
    </MemoryRouter>,
  );
}

function rows(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>("ul.j-list.a-list > li")];
}

/** The pills, each one with its label and whether it is the one selected. */
function pills(): { label: string; on: boolean }[] {
  return [...document.querySelectorAll(".a-bar .j-chip")].map((item) => ({
    label: item.textContent ?? "",
    on: item.className.includes("on"),
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the assignments page", () => {
  it("gives one row an assignment, with its marks and its numbers", async () => {
    stubList();

    page();

    expect(await screen.findByText("Clean up the docs")).toBeDefined();
    expect(rows().length).toEqual(1);
    const first = rows()[0] as HTMLElement;
    expect(first.querySelector(".j-list-title")?.getAttribute("href")).toEqual("/assignments/7");
    expect(first.querySelector(".a-row-line")?.textContent).toEqual("The docs of the viewer.");
    expect(first.querySelector(".j-list-sub")?.textContent).toEqual(
      "started 2h ago · 1h 20m · 12k tokens",
    );
    expect(first.querySelector(".j-badge.accent")?.textContent).toEqual("running");
    expect(document.title).toEqual("assignments");
  });

  it("asks the viewer for no state, and marks the one it picked", async () => {
    const asked = stubList();

    page();

    await screen.findByText("Clean up the docs");
    expect(asked).toContain("/api/v1/assignments");
    expect(pills()).toEqual([
      { label: "Drafts", on: false },
      { label: "In progress", on: true },
      { label: "Completed", on: false },
    ]);
  });

  it("asks for the state of the address, and each pill is a link to one", async () => {
    const asked = stubList(json({ ...LIST, state: "draft", items: [DRAFT] }));

    page("?state=draft");

    await screen.findByText("Book the hall");
    expect(asked).toContain("/api/v1/assignments?state=draft");
    expect(screen.getByText("Completed").getAttribute("href")).toEqual(
      "/assignments?state=completed",
    );
    expect(screen.getByText("Drafts").getAttribute("aria-current")).toEqual("true");
    const draft = rows()[0] as HTMLElement;
    expect(draft.querySelector(".a-row-line.j-muted")?.textContent).toEqual(
      'Say "start assignment 9".',
    );
    expect(draft.querySelector(".j-list-sub")?.textContent).toEqual("created 5m ago");
  });

  it("marks the assignment that asks a question", async () => {
    stubList(json({ ...LIST, items: [{ ...ROW, asks: true, status: "waiting" }] }));

    page();

    expect(await screen.findByText("question")).toBeDefined();
    expect(document.querySelector(".j-badge.warn.plain")?.textContent).toEqual("question");
  });

  it("links the saved assignments", async () => {
    stubList();

    page();

    await screen.findByText("Clean up the docs");
    expect(screen.getByText("Saved").getAttribute("href")).toEqual("/assignments/saved");
  });

  it("says what the state holds nothing", async () => {
    stubList(json({ ...LIST, items: [] }));

    page();

    expect(await screen.findByText("No assignment is in progress.")).toBeDefined();
    expect(document.querySelector("ul.j-list")).toBeNull();
  });

  it("says the manager is not configured, and selects no pill", async () => {
    stubList(
      json({ ...LIST, configured: false, available: false, state: null, items: [] }),
    );

    page();

    expect(await screen.findByText(NOT_CONFIGURED)).toBeDefined();
    expect(pills().filter((item) => item.on)).toEqual([]);
    expect(document.querySelector("ul.j-list")).toBeNull();
  });

  it("says the manager did not answer, and selects no pill", async () => {
    stubList(json({ ...LIST, available: false, state: null, items: [] }));

    page();

    expect(await screen.findByText(NO_ANSWER)).toBeDefined();
    expect(pills().filter((item) => item.on)).toEqual([]);
  });

  it("says what the viewer said when the list does not load", async () => {
    stubList(json({ error: "not found" }, 404));

    page();

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("ul.j-list")).toBeNull();
  });
});

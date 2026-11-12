import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Saved from "./Saved";
import { NOT_CONFIGURED, NO_ANSWER } from "./Assignments";
import type { SavedList, SavedRow } from "../assignments/data";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "assignments", label: "Assignments", path: "/assignments" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const ROW: SavedRow = {
  id: 3,
  url: "/assignments/saved/3",
  name: "docs-cleanup",
  title: "Clean up the docs",
  schedule: "every Monday at 9am",
  last_run: {
    id: 7,
    url: "/assignments/7",
    status: "done",
    status_label: "done",
    when: "24h ago",
  },
  run_count: 2,
  open_comments: 1,
};

const NEVER: SavedRow = {
  ...ROW,
  id: 4,
  url: "/assignments/saved/4",
  name: "weekly-audit",
  title: "",
  schedule: "",
  last_run: null,
  run_count: 0,
  open_comments: 0,
};

const LIST: SavedList = {
  configured: true,
  available: true,
  items: [ROW, NEVER],
  empty: "No saved assignment. Ask Joshua to save an assignment by name to run it again.",
  timezone: "UTC",
  now: "2026-11-09T15:00:00+00:00",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubList(list: Response = json(LIST)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    if (path.startsWith("/api/v1/assignments/saved")) {
      return Promise.resolve(list.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page() {
  return render(
    <MemoryRouter initialEntries={["/assignments/saved"]}>
      <Routes>
        <Route path="/assignments/saved" element={<Saved />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the saved assignments", () => {
  it("gives one row a saved assignment, with its runs and its last run", async () => {
    const asked = stubList();

    page();

    expect(await screen.findByText("docs-cleanup")).toBeDefined();
    expect(asked).toContain("/api/v1/assignments/saved");
    const rows = [...document.querySelectorAll("ul.j-list > li")];
    expect(rows.length).toEqual(2);
    const first = rows[0] as HTMLElement;
    expect(first.querySelector("a.j-list-title")?.getAttribute("href")).toEqual(
      "/assignments/saved/3",
    );
    expect(first.querySelector(".j-list-sub")?.textContent).toEqual("Clean up the docs");
    expect(first.textContent).toContain("2 runs");
    expect(first.textContent).toContain("every Monday at 9am");
    expect(first.querySelector(".j-badge.ok")?.textContent).toEqual("done");
    expect(first.querySelector(".a-comments")?.textContent).toEqual("1 comment for Joshua");
  });

  it("links the last run to its assignment", async () => {
    stubList();

    page();

    const link = await screen.findByText("last run");
    expect(link.getAttribute("href")).toEqual("/assignments/7");
  });

  it("says nothing of a saved assignment that never ran", async () => {
    stubList();

    page();

    await screen.findByText("weekly-audit");
    const row = document.querySelectorAll("ul.j-list > li")[1] as HTMLElement;
    expect(row.textContent).toContain("0 runs");
    expect(row.querySelector(".j-badge")).toBeNull();
  });

  it("names the assignments in the crumbs", async () => {
    stubList();

    page();

    await screen.findByText("docs-cleanup");
    expect(document.querySelector("nav.j-crumbs a")?.getAttribute("href")).toEqual("/assignments");
    expect(document.title).toEqual("saved assignments");
  });

  it("says what to do when the person saved nothing yet", async () => {
    stubList(json({ ...LIST, items: [] }));

    page();

    expect(await screen.findByText(LIST.empty)).toBeDefined();
  });

  it("says the manager is not configured, and shows no row", async () => {
    stubList(json({ ...LIST, configured: false, available: false, items: [] }));

    page();

    expect(await screen.findByText(NOT_CONFIGURED)).toBeDefined();
    expect(document.querySelector("ul.j-list")).toBeNull();
  });

  it("says the manager did not answer", async () => {
    stubList(json({ ...LIST, available: false, items: [] }));

    page();

    expect(await screen.findByText(NO_ANSWER)).toBeDefined();
  });

  it("says what the viewer said when the read failed", async () => {
    stubList(json({ error: "not found" }, 404));

    page();

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
  });
});

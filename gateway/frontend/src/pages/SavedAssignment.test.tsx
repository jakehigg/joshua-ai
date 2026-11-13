import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import SavedAssignment from "./SavedAssignment";
import { NOT_CONFIGURED } from "./Assignments";
import type { SavedData } from "../assignments/data";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "assignments", label: "Assignments", path: "/assignments" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const LINES = ["title: Clean up the docs", "goal: |-", "  Clean up the docs.", "model: sonnet"];

const DATA: SavedData = {
  configured: true,
  available: true,
  not_found: false,
  item: {
    id: 3,
    url: "/assignments/saved/3",
    name: "docs-cleanup",
    title: "Clean up the docs",
    schedule: "every Monday at 9am",
    last_run: {
      id: 9,
      url: "/assignments/9",
      status: "running",
      status_label: "running",
      when: "",
    },
    run_count: 5,
    open_comments: 1,
    updated: "2d ago",
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
        id: "L4",
        first_line: 4,
        last_line: 4,
        kind: "prop",
        label: "model",
        value: { kind: "text", text: "sonnet" },
        markdown: "",
        depth: 0,
        what: "the model",
      },
    ],
    lines: LINES,
    goal_offset: 2,
  },
  runs: [
    {
      id: 9,
      number: 5,
      url: "/assignments/9",
      title: "Clean up the docs",
      state: "in_progress",
      status: "running",
      status_label: "running",
      when: "started 1h ago",
      duration: "1h",
      cost: "",
      turns: 3,
      summary_line: "",
      asks: false,
      start_phrase: "",
    },
    {
      id: 7,
      number: 4,
      url: "/assignments/7",
      title: "Clean up the docs",
      state: "completed",
      status: "done",
      status_label: "done",
      when: "ended 1h ago",
      duration: "1h 20m",
      cost: "12k tokens",
      turns: 14,
      summary_line: "The docs of the viewer.",
      asks: false,
      start_phrase: "",
    },
  ],
  comments: [
    {
      id: "11",
      line_no: 4,
      line_text: "model: sonnet",
      line_now: "model: sonnet",
      stale: false,
      body: "Use opus.",
      author: "alex",
      created_at: "2026-11-09T14:50:00+00:00",
      resolved_at: null,
      resolution: null,
    },
  ],
  targets: ["definition"],
  csrf: "a-token",
  timezone: "UTC",
  now: "2026-11-09T15:00:00+00:00",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The saved assignment and the session, with one answer for every read. */
function stubOne(answer: Response = json(DATA)) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      sent.push({ path, body: JSON.parse(String(init.body)) });
      return Promise.resolve(json({ ok: true }));
    }
    if (path.startsWith("/api/v1/assignments/saved/")) {
      return Promise.resolve(answer.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return sent;
}

function page(address = "/assignments/saved/3") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/assignments/saved/:id" element={<SavedAssignment />} />
      </Routes>
    </MemoryRouter>,
  );
}

function loaded(): Promise<HTMLElement> {
  return screen.findByRole("heading", { level: 1, name: "docs-cleanup" });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("one saved assignment", () => {
  it("gives its name, what it is about, and its numbers", async () => {
    await loadedPage();

    expect(document.querySelector("p.a-lead")?.textContent).toEqual("Clean up the docs");
    expect(document.querySelector("p.j-meta")?.textContent).toEqual(
      "5 runs · every Monday at 9am · updated 2d ago",
    );
    await waitFor(() => expect(document.title).toEqual("docs-cleanup"));
  });

  it("names the assignments and the saved ones in the crumbs", async () => {
    await loadedPage();

    const crumbs = [...document.querySelectorAll("nav.j-crumbs a")];
    expect(crumbs.map((link) => link.getAttribute("href"))).toEqual([
      "/assignments",
      "/assignments/saved",
    ]);
  });

  it("shows the definition open, with its comments", async () => {
    await loadedPage();

    expect(document.querySelector("details.a-def")?.hasAttribute("open")).toBe(true);
    expect(document.querySelector("#definition .a-prop-key")?.textContent).toEqual("title");
    expect(screen.getByText("Use opus.")).toBeDefined();
  });

  it("gives each run its number and a link to its assignment", async () => {
    await loadedPage();

    const rows = [...document.querySelectorAll("#runs ul.j-list > li")];
    expect(rows.map((row) => row.querySelector("a")?.textContent)).toEqual([
      "Run 5: Clean up the docs",
      "Run 4: Clean up the docs",
    ]);
    expect(rows[0]?.querySelector("a")?.getAttribute("href")).toEqual("/assignments/9");
    expect(rows[1]?.querySelector(".j-badge.ok")?.textContent).toEqual("done");
    expect(rows[1]?.textContent).toContain("12k tokens");
  });

  it("says so when the saved assignment never ran", async () => {
    stubOne(json({ ...DATA, runs: [] }));

    page();

    await loaded();
    expect(screen.getByText("No run yet.")).toBeDefined();
  });

  it("sends a comment on a line of the definition", async () => {
    const sent = stubOne();

    page();
    await loaded();
    fireEvent.click(screen.getByTitle("Comment on the title"));
    fireEvent.change(screen.getByLabelText("Your comment on the title"), {
      target: { value: "Name the pages." },
    });
    fireEvent.click(screen.getByText("Comment"));

    await waitFor(() => expect(sent.length).toEqual(1));
    expect(sent[0]).toEqual({
      path: "/api/v1/assignments/saved/3/comments",
      body: {
        target: "definition",
        csrf: "a-token",
        line_no: 1,
        line_text: "title: Clean up the docs",
        body: "Name the pages.",
      },
    });
  });

  it("offers no + when the manager gave no definition text", async () => {
    stubOne(json({ ...DATA, csrf: null, definition: { units: [], lines: [], goal_offset: 0 } }));

    page();

    await loaded();
    expect(document.querySelector("button.v-plus")).toBeNull();
    expect(screen.getByText("The definition is empty.")).toBeDefined();
  });

  it("says the manager is not configured, and shows no saved assignment", async () => {
    stubOne(
      json({
        ...DATA,
        configured: false,
        available: false,
        item: null,
        definition: null,
        runs: [],
        comments: [],
        csrf: null,
      }),
    );

    page();

    expect(await screen.findByText(NOT_CONFIGURED)).toBeDefined();
    expect(document.querySelector("details.a-def")).toBeNull();
  });

  it("says what the viewer said about a row of another person", async () => {
    stubOne(json({ error: "not found" }, 404));

    page("/assignments/saved/99");

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
  });
});

/** The page once the saved assignment is there. */
async function loadedPage(): Promise<void> {
  stubOne();
  page();
  await loaded();
}

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Documents, { EMPTY_TEXT, type DocumentList } from "./Documents";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "documents", label: "Documents", path: "/documents" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const ANSWER: DocumentList = {
  status: "active",
  kind: null,
  counts: { active: 2, draft: 1, final: 0, archived: 0 },
  kinds: ["plan", "report", "spec", "artifact", "note"],
  documents: [
    {
      path: "documents/alex/party.md",
      url: "/documents/alex/party",
      title: "Party plan",
      kind: "plan",
      status: "active",
      owner: "alex",
      owner_label: "yours",
      summary: "The plan for the party on Saturday.",
      updated: "2026-11-09T12:00:00+00:00",
      open_comments: 2,
    },
    {
      path: "documents/shared/meals.md",
      url: "/documents/shared/meals",
      title: "Meal plan",
      kind: "note",
      status: "active",
      owner: "shared",
      owner_label: "shared",
      summary: "",
      updated: null,
      open_comments: 0,
    },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The list and the session. The answer is the same for every query. */
function stubList(list: Response = json(ANSWER)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    if (path.startsWith("/api/v1/documents")) {
      return Promise.resolve(list.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(query = "") {
  return render(
    <MemoryRouter initialEntries={[`/documents${query}`]}>
      <Routes>
        <Route path="/documents" element={<Documents />} />
      </Routes>
    </MemoryRouter>,
  );
}

function rows(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>("ul.j-list.d-list > li")];
}

function titles(): string[] {
  return rows().map((row) => row.querySelector(".j-list-title")?.textContent ?? "");
}

/** One chip of a group of the bar, by its label. */
function chip(label: string): HTMLButtonElement {
  return screen.getByText(label).closest("button") as HTMLButtonElement;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the documents page", () => {
  it("gives one row a document, with its marks and its summary", async () => {
    stubList();

    page();

    expect(await screen.findByText("Party plan")).toBeDefined();
    expect(titles()).toEqual(["Party plan", "Meal plan"]);
    expect(document.querySelector("table")).toBeNull();
    const first = rows()[0] as HTMLElement;
    expect(first.querySelector(".j-list-title")?.getAttribute("href")).toEqual(
      "/documents/alex/party",
    );
    expect(first.querySelector(".a-row-line")?.textContent).toEqual(
      "The plan for the party on Saturday.",
    );
    expect(first.querySelector(".j-list-sub")?.textContent).toContain("plan");
    expect(first.querySelector(".j-list-sub")?.textContent).toContain("yours");
    expect(first.querySelector(".j-list-sub time")?.getAttribute("datetime")).toEqual(
      "2026-11-09T12:00:00+00:00",
    );
    expect(first.querySelector(".a-comments")?.textContent).toEqual("2 comments for Joshua");
    expect(first.querySelector(".j-badge.accent")).not.toBeNull();
    expect(document.title).toEqual("documents");
  });

  it("gives a document with no comment no badge, and no time with no change", async () => {
    stubList();

    page();

    await screen.findByText("Meal plan");
    const second = rows()[1] as HTMLElement;
    expect(second.querySelector(".a-comments")).toBeNull();
    expect(second.querySelector("time")).toBeNull();
    expect(second.querySelector(".a-row-line")).toBeNull();
    expect(second.querySelector(".j-list-sub")?.textContent).toContain("shared");
  });

  it("marks the status of the list and asks the viewer for the one in the query", async () => {
    const asked = stubList(json({ ...ANSWER, status: "draft" }));

    page("?status=draft&kind=plan");

    await screen.findByText("Party plan");
    expect(asked).toContain("/api/v1/documents?status=draft&kind=plan");
    expect(chip("Drafts").className).toEqual("j-chip on");
    expect(chip("Active").className).toEqual("j-chip");
  });

  it("writes the status the person picks into the address", async () => {
    const asked = stubList();

    page();

    await screen.findByText("Party plan");
    fireEvent.click(chip("Final"));

    expect(asked).toContain("/api/v1/documents");
    expect(asked).toContain("/api/v1/documents?status=final");
  });

  it("writes the kind the person picks beside the status", async () => {
    const asked = stubList();

    page("?status=final");

    await screen.findByText("Party plan");
    fireEvent.click(chip("report"));

    expect(asked).toContain("/api/v1/documents?status=final&kind=report");
  });

  it("gives a chip for each kind, and all kinds when the query names none", async () => {
    stubList();

    page();

    await screen.findByText("Party plan");
    expect(
      [...document.querySelectorAll(".d-kinds button")].map((item) => item.textContent),
    ).toEqual(["all kinds", "plan", "report", "spec", "artifact", "note"]);
    expect(chip("all kinds").className).toEqual("j-chip on");
  });

  it("says there is nothing yet when no document is there at all", async () => {
    stubList(json({ ...ANSWER, counts: {}, documents: [] }));

    page();

    expect(await screen.findByText(EMPTY_TEXT)).toBeDefined();
  });

  it("says what the status holds nothing when another one holds something", async () => {
    stubList(json({ ...ANSWER, status: "final", documents: [] }));

    page("?status=final");

    expect(await screen.findByText("No final document.")).toBeDefined();
  });

  it("names the kind in the empty text of a kind", async () => {
    stubList(json({ ...ANSWER, kind: "report", documents: [] }));

    page("?kind=report");

    expect(await screen.findByText("No active document of the kind report.")).toBeDefined();
  });

  it("says what the viewer said when the list does not load", async () => {
    stubList(json({ error: "not found" }, 404));

    page();

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("ul.j-list")).toBeNull();
  });
});

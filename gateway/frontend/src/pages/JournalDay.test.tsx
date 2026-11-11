import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import JournalDay, { type JournalDayData } from "./JournalDay";
import type { JournalEntry } from "../journal/Entry";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "journal", label: "Journal", path: "/journal" }],
  chat_url: "/chat/alex",
  links: [],
  timezone: "America/New_York",
};

const ENTRY: JournalEntry = {
  day: "2026-11-10",
  day_url: "/journal/2026/11/10",
  slug: "dinner",
  title: "Dinner at home",
  source: "agent",
  people: [{ id: "alex", name: "Alex" }],
  markdown: "We made pizza.\n",
  edit_url: "/edit/journal/2026/11/10/dinner.md",
};

const DAY: JournalDayData = {
  day: "2026-11-10",
  can_edit: true,
  entries: [ENTRY],
  older: { day: "2026-11-08", url: "/journal/2026/11/08" },
  newer: { day: "2026-11-11", url: "/journal/2026/11/11" },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The session and the day. The list it gives back holds each address asked. */
function stubRoutes(day: Response): string[] {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    if (path.startsWith("/api/v1/journal")) {
      asked.push(path);
      return Promise.resolve(day.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(address = "/journal/2026/11/10") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/journal/:year/:month/:day" element={<JournalDay />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("one day of the journal", () => {
  it("asks for the day of the address and shows its entries", async () => {
    const asked = stubRoutes(json(DAY));

    page();

    expect(await screen.findByText("Dinner at home")).toBeDefined();
    expect(asked).toEqual(["/api/v1/journal/2026/11/10"]);
    expect(document.querySelector("h1.j-doc-title")?.textContent).toEqual(
      "Tuesday 10 November 2026",
    );
    expect(screen.getByText("edit this entry")).toBeDefined();
    expect(document.title).toEqual("2026-11-10");
  });

  it("links the day before, the day after and the month", async () => {
    stubRoutes(json(DAY));

    page();

    await screen.findByText("Dinner at home");
    const pager = document.querySelector("p.pager");
    const links = [...(pager?.querySelectorAll("a") ?? [])];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/journal/2026/11/08",
      "/journal?month=2026-11",
      "/journal/2026/11/11",
    ]);
    expect(links[0]?.textContent).toEqual("← Sunday 8 November 2026");
    expect(links[1]?.textContent).toEqual("November 2026");
    expect(links[2]?.textContent).toEqual("Wednesday 11 November 2026 →");
  });

  it("says a day holds nothing, and shows only the side that has a day", async () => {
    stubRoutes(json({ ...DAY, entries: [], newer: null }));

    page();

    const line = await screen.findByText("Nothing on this day.");
    expect(line.className).toEqual("j-empty");
    const links = [...(document.querySelector("p.pager")?.querySelectorAll("a") ?? [])];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/journal/2026/11/08",
      "/journal?month=2026-11",
    ]);
  });

  it("goes to the entry of the fragment once the entries are here", async () => {
    stubRoutes(json(DAY));
    const seen: string[] = [];
    Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
      seen.push(this.id);
    };

    page("/journal/2026/11/10#dinner");

    await screen.findByText("Dinner at home");
    expect(seen).toEqual(["dinner"]);
  });

  it("says what the viewer said when the day is not a day", async () => {
    stubRoutes(json({ error: "not found" }, 404));

    page("/journal/2026/02/31");

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("article.j-entry")).toBeNull();
  });
});

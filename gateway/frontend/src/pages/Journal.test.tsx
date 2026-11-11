import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Journal, { type JournalFeed } from "./Journal";
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

const DAY_PAGE: JournalEntry = {
  day: "2026-11-10",
  day_url: "/journal/2026/11/10",
  slug: "2026-11-10",
  title: "The day",
  source: "nightly",
  people: [{ id: "alex", name: "Alex" }],
  markdown: "Alex booked the flights.\n",
  edit_url: "/edit/journal/2026/11/10/2026-11-10.md",
};

const ENTRY: JournalEntry = {
  day: "2026-11-08",
  day_url: "/journal/2026/11/08",
  slug: "pottery-class",
  title: "Pottery class",
  source: "agent",
  people: [{ id: "mia", name: "Mia" }],
  markdown: "Mia started the class.\n",
  edit_url: "/edit/journal/2026/11/08/pottery-class.md",
};

const FEED: JournalFeed = {
  month: "2026-11",
  available: ["2026-11", "2026-09"],
  people: [
    { id: "alex", name: "Alex" },
    { id: "mia", name: "Mia" },
  ],
  person: null,
  can_edit: true,
  entries: [DAY_PAGE, ENTRY],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The session and the feed. The list it gives back holds each address asked. */
function stubRoutes(feed: Response): string[] {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    if (path.startsWith("/api/v1/journal")) {
      asked.push(path);
      return Promise.resolve(feed.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

/** The address of the page, so a test reads what a filter wrote. */
function Address() {
  const location = useLocation();
  return <p>{`at ${location.pathname}${location.search}`}</p>;
}

function page(address = "/journal") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route
          path="/journal"
          element={
            <>
              <Journal />
              <Address />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the journal of one month", () => {
  it("asks for the month and the person of the address", async () => {
    const asked = stubRoutes(json({ ...FEED, month: "2026-09", person: "mia" }));

    page("/journal?month=2026-09&person=mia");

    expect(await screen.findByText("Pottery class")).toBeDefined();
    expect(asked).toEqual(["/api/v1/journal?month=2026-09&person=mia"]);
  });

  it("shows the months with an entry, the people and the entries newest first", async () => {
    stubRoutes(json(FEED));

    page();

    expect(await screen.findByText("The day")).toBeDefined();
    const select = document.querySelector("select") as HTMLSelectElement;
    expect(select.value).toEqual("2026-11");
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "November 2026",
      "September 2026",
    ]);
    // The filter of a person is a button; the chips of an entry are links.
    const chips = screen.getAllByRole("button");
    expect(chips.map((chip) => chip.textContent)).toEqual(["everyone", "Alex", "Mia"]);
    const titles = [...document.querySelectorAll("h3.j-entry-title")];
    expect(titles.map((title) => title.textContent)).toEqual(["The day", "Pottery class"]);
    expect(document.title).toEqual("journal");
  });

  it("writes the month of the picker into the address", async () => {
    stubRoutes(json(FEED));

    page();

    await screen.findByText("The day");
    fireEvent.change(document.querySelector("select") as HTMLSelectElement, {
      target: { value: "2026-09" },
    });

    expect(await screen.findByText("at /journal?month=2026-09")).toBeDefined();
  });

  it("writes the person of the filter into the address, and everyone drops it", async () => {
    stubRoutes(json(FEED));

    page("/journal?month=2026-11");

    await screen.findByText("The day");
    fireEvent.click(screen.getByRole("button", { name: "Mia" }));
    expect(await screen.findByText("at /journal?month=2026-11&person=mia")).toBeDefined();

    await screen.findByText("The day");
    fireEvent.click(screen.getByRole("button", { name: "everyone" }));
    expect(await screen.findByText("at /journal?month=2026-11")).toBeDefined();
  });

  it("keeps a month with no entry in the picker, and says the month is empty", async () => {
    stubRoutes(json({ ...FEED, month: "2026-10", entries: [] }));

    page("/journal?month=2026-10");

    const line = await screen.findByText(/Nothing in the journal yet/);
    expect(line.className).toEqual("j-empty");
    const select = document.querySelector("select") as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual([
      "2026-11",
      "2026-10",
      "2026-09",
    ]);
    expect(select.value).toEqual("2026-10");
  });

  it("says what the viewer said when the journal is refused", async () => {
    stubRoutes(json({ error: "forbidden" }, 403));

    page();

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("select")).toBeNull();
  });
});

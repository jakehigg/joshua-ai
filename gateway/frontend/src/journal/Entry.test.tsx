import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import Entry, { dayLabel, monthLabel, type JournalEntry } from "./Entry";

const ENTRY: JournalEntry = {
  day: "2026-11-10",
  day_url: "/journal/2026/11/10",
  slug: "dinner",
  title: "Dinner at home",
  source: "agent",
  people: [
    { id: "alex", name: "Alex" },
    { id: "mia", name: "Mia" },
  ],
  markdown: "We made pizza.\n\n<script>alert(1)</script>\n",
  edit_url: "/edit/journal/2026/11/10/dinner.md",
};

const DAY_PAGE: JournalEntry = {
  ...ENTRY,
  slug: "2026-11-10",
  title: "The day",
  source: "nightly",
  people: [],
  markdown: "",
  edit_url: "/edit/journal/2026/11/10/2026-11-10.md",
};

function show(entry: JournalEntry, canEdit = false) {
  return render(
    <MemoryRouter initialEntries={["/journal"]}>
      <Entry entry={entry} canEdit={canEdit} />
    </MemoryRouter>,
  );
}

describe("one entry of the journal", () => {
  it("shows the title, the day, the source, the people and the text", () => {
    show(ENTRY);

    const article = document.querySelector("article.j-entry");
    expect(article?.getAttribute("id")).toEqual("dinner");
    const title = screen.getByText("Dinner at home");
    expect(title.closest("h3")?.className).toEqual("j-entry-title");
    expect(title.getAttribute("href")).toEqual("/journal/2026/11/10#dinner");
    const when = document.querySelector("p.j-entry-when");
    expect(when?.textContent).toEqual("Tuesday 10 November 2026");
    expect(when?.querySelector("a")?.getAttribute("href")).toEqual("/journal/2026/11/10");
    expect(screen.getByText("agent").className).toEqual("j-badge");
    const chips = document.querySelectorAll("div.j-chips a.j-chip");
    expect([...chips].map((chip) => chip.getAttribute("href"))).toEqual([
      "/journal?person=alex",
      "/journal?person=mia",
    ]);
    expect(screen.getByText("We made pizza.")).toBeDefined();
  });

  it("renders the text as prose, and the HTML in it is never markup", () => {
    show(ENTRY);

    const prose = document.querySelector("article.j-prose");
    expect(prose).not.toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(prose?.textContent).not.toContain("alert(1)");
  });

  it("marks the page of a day, and says so when it holds no text", () => {
    show(DAY_PAGE);

    expect(screen.getByText("nightly").className).toEqual("j-badge accent");
    expect(screen.getByText("(empty)").className).toEqual("j-faint");
    expect(document.querySelector("div.j-chips")).toBeNull();
  });

  it("gives a member the editor of the entry", () => {
    show(ENTRY, true);

    expect(screen.getByText("edit this entry").getAttribute("href")).toEqual(
      "/edit/journal/2026/11/10/dinner.md",
    );
  });

  it("gives a guest no editor", () => {
    show(ENTRY);

    expect(screen.queryByText("edit this entry")).toBeNull();
  });

  it("names a day and a month in the way a person reads them", () => {
    expect(dayLabel("2026-11-10")).toEqual("Tuesday 10 November 2026");
    expect(dayLabel("2026-01-01")).toEqual("Thursday 1 January 2026");
    expect(monthLabel("2026-09")).toEqual("September 2026");
    expect(monthLabel("2026-01")).toEqual("January 2026");
  });
});

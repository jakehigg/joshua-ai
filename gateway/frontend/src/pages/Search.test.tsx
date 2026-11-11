import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Search, { ASK_TEXT, NO_CLOSE_TEXT, type SearchAnswer } from "./Search";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "search", label: "Search", path: "/search" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const ANSWER: SearchAnswer = {
  query: "pizza",
  meaning_configured: true,
  meaning: [
    {
      url: "/wiki/journal/2026/09/04/oatmeal.md",
      label: "Breakfast",
      source: "journal · 2026-09-04",
      snippet: "Oats with honey.",
    },
  ],
  words: [{ url: "/wiki/pizza.md", label: "Pizza", snippet: "Dough and sauce." }],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The session and the search. The list it gives back holds each address asked. */
function stubRoutes(answer: Response): string[] {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    if (path.startsWith("/api/v1/search")) {
      asked.push(path);
      return Promise.resolve(answer.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(address = "/search?q=pizza") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/search" element={<Search />} />
      </Routes>
    </MemoryRouter>,
  );
}

function titles(): string[] {
  return [...document.querySelectorAll("a.j-list-title.wrap")].map(
    (hit) => hit.textContent ?? "",
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the search page", () => {
  it("asks for the query of the address and shows the meaning half first", async () => {
    const asked = stubRoutes(json(ANSWER));

    page("/search?q=pizza%20dough");

    expect(await screen.findByText("Pizza")).toBeDefined();
    expect(asked).toEqual(["/api/v1/search?q=pizza%20dough"]);
    const headings = [...document.querySelectorAll("h2")].map((item) => item.textContent);
    expect(headings).toEqual(["By meaning", "By words"]);
    expect(titles()).toEqual(["Breakfast", "Pizza"]);
    expect(document.querySelector("a.j-list-title")?.getAttribute("href")).toEqual(
      "/wiki/journal/2026/09/04/oatmeal.md",
    );
    expect(screen.getByText("journal · 2026-09-04").className).toEqual("j-list-sub");
    expect(screen.getByText("Dough and sauce.").className).toEqual("j-list-sub hit-text");
    expect(document.title).toEqual("search");
  });

  it("asks for a query when the address has none, and reads nothing", () => {
    const asked = stubRoutes(json(ANSWER));

    page("/search");

    const line = screen.getByText(ASK_TEXT);
    expect(line.className).toEqual("j-empty");
    expect(asked).toEqual([]);
  });

  it("shows the words half alone when core did not answer", async () => {
    stubRoutes(json({ ...ANSWER, meaning: null }));

    page();

    expect(await screen.findByText("Pizza")).toBeDefined();
    expect(document.querySelectorAll("h2").length).toEqual(0);
    expect(screen.queryByText(NO_CLOSE_TEXT)).toBeNull();
  });

  it("says core found nothing close, and says what words found nothing", async () => {
    stubRoutes(json({ ...ANSWER, meaning: [], words: [] }));

    page("/search?q=pizza");

    expect(await screen.findByText(NO_CLOSE_TEXT)).toBeDefined();
    expect(screen.getByText("No match for pizza.").className).toEqual("j-empty");
  });

  it("says what the viewer said when the search is refused", async () => {
    stubRoutes(json({ error: "forbidden" }, 403));

    page();

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
  });

  it("holds the query in the field of the app bar", async () => {
    stubRoutes(json(ANSWER));

    page("/search?q=pizza");

    await screen.findByText("Pizza");
    const field = document.querySelector('form.v-search input[name="q"]') as HTMLInputElement;
    expect(field.value).toEqual("pizza");
  });
});

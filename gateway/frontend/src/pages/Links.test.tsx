import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Links, { ABOUT_TEXT, EMPTY_TEXT } from "./Links";
import type { LinksData } from "../shell/LinksNav";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const PANTRY = "https://pantry.example.test/";
const WEATHER = "https://example.com/weather";

const ANSWER: LinksData = {
  links: [
    { title: "Pantry", url: PANTRY, icon: "box" },
    { title: "Weather", url: WEATHER, icon: "cloud" },
  ],
  icons: ["link", "box", "cloud", "music"],
  can_edit: true,
  csrf: "a-token",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The session, the list, and one answer of the write. The page and the links
 * group of the sidebar both read the list, so the stub clones it. The list it
 * gives back holds the body of each save the page posted.
 */
function stubRoutes(list: Response = json(ANSWER), saved?: Response) {
  const sent: unknown[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path === "/api/v1/links" && init?.method === "POST") {
      sent.push(JSON.parse(String(init.body)));
      return Promise.resolve(saved ?? json({ ok: true, links: [] }));
    }
    if (path === "/api/v1/links") {
      return Promise.resolve(list.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return sent;
}

function page() {
  return render(
    <MemoryRouter initialEntries={["/links"]}>
      <Routes>
        <Route path="/links" element={<Links />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** One field of the editor, which only the page has. */
function field(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

/** The rows of the saved list of the page, never of the sidebar. */
function rows(): HTMLAnchorElement[] {
  return [...document.querySelectorAll<HTMLAnchorElement>("ul.v-link-list li a")];
}

function titles(): string[] {
  return rows().map((row) => row.querySelector(".j-list-title")?.textContent ?? "");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the links page", () => {
  it("says what the links are, and shows each one in a new tab", async () => {
    stubRoutes();

    page();

    expect(await screen.findByText(ABOUT_TEXT)).toBeDefined();
    expect(titles()).toEqual(["Pantry", "Weather"]);
    expect(rows()[0]?.getAttribute("href")).toEqual(PANTRY);
    expect(rows()[0]?.getAttribute("target")).toEqual("_blank");
    expect(rows()[0]?.getAttribute("rel")).toEqual("noopener noreferrer");
    expect(rows()[0]?.querySelector("svg.j-icon")).not.toBeNull();
    expect(document.title).toEqual("links");
  });

  it("says there is nothing yet with no link", async () => {
    stubRoutes(json({ ...ANSWER, links: [] }));

    page();

    expect(await screen.findByText(EMPTY_TEXT)).toBeDefined();
  });

  it("gives a member one row per link, with its title, URL and icon", async () => {
    stubRoutes();

    page();

    expect(await screen.findByText("Save links")).toBeDefined();
    expect(document.querySelectorAll("fieldset.v-link-row").length).toEqual(2);
    expect(field("Title for row 1").value).toEqual("Pantry");
    expect(field("URL for row 1").value).toEqual(PANTRY);
    expect((screen.getByLabelText("Icon for row 1") as HTMLSelectElement).value).toEqual("box");
    expect(
      [...document.querySelectorAll('select[aria-label="Icon for row 1"] option')].map(
        (option) => option.textContent,
      ),
    ).toEqual(["link", "box", "cloud", "music"]);
  });

  it("shows a guest the list and no editor", async () => {
    stubRoutes(json({ ...ANSWER, can_edit: false, csrf: null }));

    page();

    expect(await screen.findByText(ABOUT_TEXT)).toBeDefined();
    expect(titles()).toEqual(["Pantry", "Weather"]);
    expect(document.querySelector("form.v-links-form")).toBeNull();
    expect(screen.queryByText("Save links")).toBeNull();
  });

  it("adds a row, changes it, and posts the whole list with the token", async () => {
    const sent = stubRoutes();

    page();

    await screen.findByText("Save links");
    fireEvent.click(screen.getByText("Add a link"));
    fireEvent.change(field("Title for row 3"), { target: { value: "Vinyl" } });
    fireEvent.change(field("URL for row 3"), { target: { value: "http://vinyl.test/" } });
    fireEvent.change(screen.getByLabelText("Icon for row 3"), { target: { value: "music" } });
    fireEvent.click(screen.getByText("Save links"));

    expect(sent).toEqual([
      {
        csrf: "a-token",
        links: [
          { title: "Pantry", url: PANTRY, icon: "box" },
          { title: "Weather", url: WEATHER, icon: "cloud" },
          { title: "Vinyl", url: "http://vinyl.test/", icon: "music" },
        ],
      },
    ]);
  });

  it("moves a row, and posts the order the rows now have", async () => {
    const sent = stubRoutes();

    page();

    await screen.findByText("Save links");
    fireEvent.click(screen.getByLabelText("Move row 1 down"));
    fireEvent.click(screen.getByText("Save links"));

    expect(field("Title for row 1").value).toEqual("Weather");
    expect(sent).toEqual([
      {
        csrf: "a-token",
        links: [
          { title: "Weather", url: WEATHER, icon: "cloud" },
          { title: "Pantry", url: PANTRY, icon: "box" },
        ],
      },
    ]);
  });

  it("removes a row, and posts the list without it", async () => {
    const sent = stubRoutes();

    page();

    await screen.findByText("Save links");
    fireEvent.click(screen.getByLabelText("Remove row 2"));
    fireEvent.click(screen.getByText("Save links"));

    expect(document.querySelectorAll("fieldset.v-link-row").length).toEqual(1);
    expect(sent).toEqual([
      { csrf: "a-token", links: [{ title: "Pantry", url: PANTRY, icon: "box" }] },
    ]);
  });

  it("shows the list the viewer saved", async () => {
    stubRoutes(
      json(ANSWER),
      json({ ok: true, links: [{ title: "Pantry inventory", url: PANTRY, icon: "cart" }] }),
    );

    page();

    await screen.findByText("Save links");
    fireEvent.click(screen.getByText("Save links"));

    expect(await screen.findByText("Pantry inventory")).toBeDefined();
    expect(titles()).toEqual(["Pantry inventory"]);
    expect(field("Title for row 1").value).toEqual("Pantry inventory");
  });

  it("keeps the rows and says what the viewer said on a bad row", async () => {
    stubRoutes(json(ANSWER), json({ error: "row 2: a link needs a URL" }, 400));

    page();

    await screen.findByText("Save links");
    fireEvent.change(field("URL for row 2"), { target: { value: "" } });
    fireEvent.click(screen.getByText("Save links"));

    const line = await screen.findByText("row 2: a link needs a URL");
    expect(line.className).toEqual("j-error");
    expect(document.querySelectorAll("fieldset.v-link-row").length).toEqual(2);
    expect(titles()).toEqual(["Pantry", "Weather"]);
  });

  it("says what the viewer said when the list does not load", async () => {
    stubRoutes(json({ error: "not found" }, 404));

    page();

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("form.v-links-form")).toBeNull();
  });
});

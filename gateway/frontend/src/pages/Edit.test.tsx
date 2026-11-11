import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Edit, { type EditData } from "./Edit";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  links: [],
  timezone: "America/New_York",
};

const WIKI: EditData = {
  kind: "wiki",
  rel: "recipes/pizza.md",
  title: "Pizza",
  subtitle: "recipes/pizza.md",
  body: "Flour and water.\n",
  kept_frontmatter: "kind: wiki\nupdated: 2026-11-10\n",
  people: null,
  cancel_url: "/wiki/recipes/pizza.md",
  csrf: "a-token",
};

const ENTRY: EditData = {
  kind: "journal",
  rel: "journal/2026/11/10/dinner.md",
  title: "Dinner",
  subtitle: "Tuesday 10 November 2026 · joshua · journal/2026/11/10/dinner.md",
  body: "We made pizza.\n",
  kept_frontmatter: null,
  people: ["alex", "sam"],
  cancel_url: "/journal/2026/11/10",
  csrf: "a-token",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The session, the editor of one page, and one answer of the save. The list
 * it gives back holds the body of each save the page posted.
 */
function stubRoutes(editor: Response, saved: Response = json({ ok: true, url: "/wiki/x.md" })) {
  const sent: unknown[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path.startsWith("/api/v1/edit")) {
      return Promise.resolve(editor);
    }
    if (path === "/api/v1/save") {
      sent.push(JSON.parse(String(init?.body)));
      return Promise.resolve(saved);
    }
    return Promise.resolve(json(SESSION));
  });
  return sent;
}

function show(rel: string) {
  return render(
    <MemoryRouter initialEntries={[`/edit/${rel}`]}>
      <Routes>
        <Route path="/edit/*" element={<Edit />} />
        <Route path="/wiki/*" element={<p>the page again</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the editor of one page", () => {
  it("asks for the path of the address, and shows the text of the answer", async () => {
    stubRoutes(json(WIKI));

    show("recipes/pizza.md");

    expect(await screen.findByText("edit: Pizza")).toBeDefined();
    expect(document.querySelector("p.j-meta")?.textContent).toEqual("recipes/pizza.md");
    const text = document.querySelector("textarea");
    expect(text?.value).toEqual("Flour and water.\n");
    expect(screen.getByText("cancel").getAttribute("href")).toEqual("/wiki/recipes/pizza.md");
    expect(document.title).toEqual("edit Pizza");
  });

  it("shows the kept frontmatter above the text, and never in it", async () => {
    stubRoutes(json(WIKI));

    show("recipes/pizza.md");

    await screen.findByText("edit: Pizza");
    expect(document.querySelector("details.kept pre")?.textContent).toEqual(
      "kind: wiki\nupdated: 2026-11-10",
    );
    expect(document.querySelector("textarea")?.value).not.toContain("kind: wiki");
  });

  it("posts the path, the token and the text, then goes to the page", async () => {
    const sent = stubRoutes(json(WIKI), json({ ok: true, url: "/wiki/recipes/pizza.md" }));

    show("recipes/pizza.md");

    await screen.findByText("edit: Pizza");
    fireEvent.change(document.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Flour, water and salt.\n" },
    });
    fireEvent.click(screen.getByText("save"));

    expect(await screen.findByText("the page again")).toBeDefined();
    expect(sent).toEqual([
      {
        csrf: "a-token",
        path: "recipes/pizza.md",
        body: "Flour, water and salt.\n",
        people: "",
      },
    ]);
  });

  it("gives a journal entry the people field, and posts the people", async () => {
    const sent = stubRoutes(json(ENTRY), json({ ok: true, url: "/journal/2026/11/10#dinner" }));
    const went: string[] = [];
    vi.stubGlobal("location", {
      assign: (url: string) => {
        went.push(url);
      },
    });

    show("journal/2026/11/10/dinner.md");

    await screen.findByText("edit: Dinner");
    const people = document.querySelector('input[name="people"]') as HTMLInputElement;
    expect(people.value).toEqual("alex, sam");
    expect(screen.getByText("cancel").getAttribute("href")).toEqual("/journal/2026/11/10");
    fireEvent.change(people, { target: { value: "alex" } });
    fireEvent.click(screen.getByText("save"));

    expect(sent).toEqual([
      {
        csrf: "a-token",
        path: "journal/2026/11/10/dinner.md",
        body: "We made pizza.\n",
        people: "alex",
      },
    ]);
    // The day of an entry is a page of the server renderer, not of this app.
    await waitFor(() => {
      expect(went).toEqual(["/journal/2026/11/10#dinner"]);
    });
  });

  it("keeps the editor and says what the viewer said when the save is refused", async () => {
    stubRoutes(json(WIKI), json({ error: "The page exceeds 256 KB." }, 400));

    show("recipes/pizza.md");

    await screen.findByText("edit: Pizza");
    fireEvent.click(screen.getByText("save"));

    const line = await screen.findByText("The page exceeds 256 KB.");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("textarea")).not.toBeNull();
  });

  it("says what the viewer said when the editor is refused", async () => {
    stubRoutes(json({ error: "forbidden" }, 403));

    show("recipes/pizza.md");

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("textarea")).toBeNull();
  });
});

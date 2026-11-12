import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import WikiPage, { type WikiPageData } from "./WikiPage";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const PAGE: WikiPageData = {
  kind: "page",
  root: "wiki",
  rel: "wiki/recipes/pizza.md",
  title: "Pizza",
  frontmatter: { kind: "wiki", updated: "2026-11-10" },
  markdown: "# Pizza\n\nFlour and water.\n",
  crumbs: [
    { title: "home", url: "/" },
    { title: "wiki", url: "/wiki/" },
    { title: "Recipes", url: "/wiki/recipes/" },
    { title: "Pizza", url: "" },
  ],
  siblings: [
    { title: "Sauces/", url: "/wiki/recipes/sauces/" },
    { title: "Ponzu", url: "/wiki/recipes/ponzu.md" },
  ],
  can_edit: true,
  edit_url: "/edit/recipes/pizza.md",
  delete_csrf: "a-token",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubSession(): void {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(SESSION)));
}

/**
 * The session, and one answer of `POST /api/v1/delete`. The list it gives
 * back holds the body of each delete the page posted.
 */
function stubDelete(answer: Response): unknown[] {
  const sent: unknown[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path === "/api/v1/delete") {
      sent.push(JSON.parse(String(init?.body)));
      return Promise.resolve(answer);
    }
    return Promise.resolve(json(SESSION));
  });
  return sent;
}

function show(page: WikiPageData) {
  stubSession();
  return render(
    <MemoryRouter initialEntries={[`/${page.rel}`]}>
      <WikiPage page={page} />
    </MemoryRouter>,
  );
}

/** The page, with the address of the answer of the delete as a route. */
function showWithHome(page: WikiPageData) {
  return render(
    <MemoryRouter initialEntries={[`/${page.rel}`]}>
      <Routes>
        <Route path="/" element={<p>the wiki index</p>} />
        <Route path="/wiki/*" element={<WikiPage page={page} />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("one page of the wiki", () => {
  it("shows the crumbs, the frontmatter, the prose and the siblings", () => {
    show(PAGE);

    const crumbs = document.querySelector("p.j-crumbs");
    expect(crumbs?.textContent).toEqual("home › wiki › Recipes › Pizza");
    expect(screen.getByText("Recipes").getAttribute("href")).toEqual("/wiki/recipes/");
    const meta = document.querySelector("p.j-meta.frontmatter");
    expect(meta?.textContent).toEqual("kind: wiki · updated: 2026-11-10");
    expect(document.querySelector("article.j-prose h1")?.textContent).toEqual("Pizza");
    expect(screen.getByText("Flour and water.")).toBeDefined();
    expect(screen.getByText("more in Recipes")).toBeDefined();
    expect(screen.getByText("Ponzu").getAttribute("href")).toEqual("/wiki/recipes/ponzu.md");
    expect(document.title).toEqual("Pizza");
  });

  it("gives a member the editor and the delete", () => {
    show(PAGE);

    expect(screen.getByText("edit this page").getAttribute("href")).toEqual(
      "/edit/recipes/pizza.md",
    );
    expect(screen.getByText("move this page to trash")).toBeDefined();
  });

  it("gives a guest no editor and no delete", () => {
    show({ ...PAGE, can_edit: false, edit_url: "/edit/recipes/pizza.md", delete_csrf: "" });

    expect(screen.queryByText("edit this page")).toBeNull();
    expect(screen.queryByText("move this page to trash")).toBeNull();
  });

  it("posts the page and its token to the delete, then goes where the answer says", async () => {
    const sent = stubDelete(json({ ok: true, url: "/" }));
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    showWithHome(PAGE);
    fireEvent.click(screen.getByText("move this page to trash"));

    expect(await screen.findByText("the wiki index")).toBeDefined();
    expect(sent).toEqual([{ csrf: "a-token", path: "wiki/recipes/pizza.md" }]);
  });

  it("asks first, and posts nothing when the person says no", () => {
    const sent = stubDelete(json({ ok: true, url: "/" }));
    const asked = vi.fn().mockReturnValue(false);
    vi.stubGlobal("confirm", asked);

    showWithHome(PAGE);
    fireEvent.click(screen.getByText("move this page to trash"));

    expect(asked).toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it("keeps the page and says what the viewer said when the delete is refused", async () => {
    stubDelete(json({ error: "forbidden" }, 403));
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    showWithHome(PAGE);
    fireEvent.click(screen.getByText("move this page to trash"));

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
    await waitFor(() => {
      expect(screen.queryByText("the wiki index")).toBeNull();
    });
  });

  it("shows a file of another root with no navigation around it", () => {
    show({
      ...PAGE,
      root: "shared",
      rel: "shared/notes.md",
      title: "shared/notes.md",
      frontmatter: {},
      markdown: "A note.\n",
      crumbs: [],
      siblings: [],
      can_edit: false,
      edit_url: "",
      delete_csrf: "",
    });

    expect(document.querySelector("p.j-crumbs")).toBeNull();
    expect(document.querySelector("p.j-meta.frontmatter")).toBeNull();
    expect(document.querySelector("section.siblings")).toBeNull();
    expect(screen.getByText("A note.")).toBeDefined();
  });
});

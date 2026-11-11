import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import WikiPage, { type WikiPageData } from "./WikiPage";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  links: [],
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

function stubSession(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(SESSION), { headers: { "Content-Type": "application/json" } }),
    ),
  );
}

function show(page: WikiPageData) {
  stubSession();
  return render(
    <MemoryRouter initialEntries={[`/${page.rel}`]}>
      <WikiPage page={page} />
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
    const form = document.querySelector("form.delete");
    // The delete still posts to the server route; task 132 adds the JSON one.
    expect(form?.getAttribute("action")).toEqual("/delete");
    expect(form?.getAttribute("method")).toEqual("post");
    expect(form?.querySelector('input[name="path"]')?.getAttribute("value")).toEqual(
      "wiki/recipes/pizza.md",
    );
    expect(form?.querySelector('input[name="csrf"]')?.getAttribute("value")).toEqual("a-token");
    expect(screen.getByText("move this page to trash")).toBeDefined();
  });

  it("gives a guest no editor and no delete", () => {
    show({ ...PAGE, can_edit: false, edit_url: "/edit/recipes/pizza.md", delete_csrf: "" });

    expect(screen.queryByText("edit this page")).toBeNull();
    expect(document.querySelector("form.delete")).toBeNull();
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

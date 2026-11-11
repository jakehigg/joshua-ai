import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import WikiTree, { Tree, isPage, type TreeNode } from "./WikiTree";

function page(title: string, rel: string): TreeNode {
  return { title, rel, url: `/wiki/${rel}`, count: 0, children: [] };
}

function folder(title: string, rel: string, children: TreeNode[], count: number): TreeNode {
  return { title, rel, url: `/wiki/${rel}/`, count, children };
}

const ROOT: TreeNode = {
  title: "wiki",
  rel: "",
  url: "/wiki/",
  count: 5,
  children: [
    page("Home", "Home.md"),
    folder("Recipes", "recipes", [page("Ponzu Sauce", "recipes/ponzu-sauce.md")], 1),
    { title: "journal", rel: "journal", url: "/journal", count: 7, children: [] },
    page("Loose", "loose-page.md"),
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the tree of the wiki", () => {
  it("shows the front page first, then each folder, then the loose pages", () => {
    render(<Tree root={ROOT} />);

    expect(screen.getByText("the front page")).toBeDefined();
    expect(screen.getByText("Home").getAttribute("href")).toEqual("/wiki/Home.md");
    expect(screen.getByText("Recipes").getAttribute("href")).toEqual("/wiki/recipes/");
    expect(screen.getByText("Ponzu Sauce").getAttribute("href")).toEqual(
      "/wiki/recipes/ponzu-sauce.md",
    );
    expect(screen.getByText("other pages")).toBeDefined();
    expect(screen.getByText("Loose")).toBeDefined();
  });

  it("shows the journal as one link with its pages counted", () => {
    render(<Tree root={ROOT} />);

    expect(screen.getByText("journal").getAttribute("href")).toEqual("/journal");
    expect(screen.getByText("7 pages")).toBeDefined();
  });

  it("opens a small folder of the root and closes the shipped docs", () => {
    const docs = folder("joshua-docs", "joshua-docs", [page("Memory", "joshua-docs/memory.md")], 3);
    render(<Tree root={{ ...ROOT, children: [ROOT.children[1]!, docs] }} />);

    const blocks = document.querySelectorAll("details");
    expect(blocks[0]?.hasAttribute("open")).toBe(true);
    expect(blocks[1]?.hasAttribute("open")).toBe(false);
  });

  it("closes a folder that holds more pages than the limit", () => {
    const big = folder("Big", "big", [page("One", "big/one.md")], 40);
    render(<Tree root={{ ...ROOT, children: [big] }} />);

    expect(document.querySelector("details")?.hasAttribute("open")).toBe(false);
  });

  it("says there is nothing yet for a wiki with no page", () => {
    render(<Tree root={{ title: "wiki", rel: "", url: "/wiki/", count: 0, children: [] }} />);

    expect(screen.getByText("nothing yet.")).toBeDefined();
  });

  it("knows a page from a folder", () => {
    expect(isPage(page("Home", "Home.md"))).toBe(true);
    expect(isPage(folder("Recipes", "recipes", [], 1))).toBe(false);
  });
});

describe("the tree that reads the viewer", () => {
  it("reads /api/v1/wiki/tree and draws it", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(ROOT), { headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<WikiTree />);

    expect(await screen.findByText("Recipes")).toBeDefined();
    expect(fetchMock.mock.calls[0]?.[0]).toEqual("/api/v1/wiki/tree");
  });

  it("says what went wrong when the viewer refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "no wiki" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    render(<WikiTree />);

    expect(await screen.findByText("no wiki")).toBeDefined();
  });
});

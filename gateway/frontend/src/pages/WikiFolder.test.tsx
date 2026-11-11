import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import WikiFolder, { type WikiFolderData } from "./WikiFolder";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  links: [],
  timezone: "America/New_York",
};

const FOLDER: WikiFolderData = {
  kind: "folder",
  rel: "recipes",
  title: "Recipes",
  crumbs: [
    { title: "home", url: "/" },
    { title: "wiki", url: "/wiki/" },
    { title: "Recipes", url: "" },
  ],
  index_markdown: "What we cook.\n\n![a](/attachments/2026/11/a.jpg)\n",
  folders: [{ title: "Sauces", url: "/wiki/recipes/sauces/" }],
  pages: [{ title: "Pizza", url: "/wiki/recipes/pizza.md" }],
  files: [{ name: "menu.pdf", url: "/api/v1/files/wiki/recipes/menu.pdf" }],
  can_create: true,
  new_url: "/new?folder=recipes",
};

function show(folder: WikiFolderData) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(SESSION), { headers: { "Content-Type": "application/json" } }),
    ),
  );
  return render(
    <MemoryRouter initialEntries={[`/wiki/${folder.rel}/`]}>
      <WikiFolder folder={folder} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("one folder of the wiki", () => {
  it("shows the crumbs, the index page, the folders, the pages and the files", () => {
    show(FOLDER);

    expect(document.querySelector("p.j-crumbs")?.textContent).toEqual("home › wiki › Recipes");
    expect(document.querySelector("h1.j-doc-title")?.textContent).toEqual("Recipes");
    expect(screen.getByText("What we cook.")).toBeDefined();
    expect(document.querySelector("article.j-prose img")?.getAttribute("src")).toEqual(
      "/api/v1/files/wiki/attachments/2026/11/a.jpg",
    );
    expect(screen.getByText("folders")).toBeDefined();
    expect(screen.getByText("Sauces/").getAttribute("href")).toEqual("/wiki/recipes/sauces/");
    expect(screen.getByText("Pizza").getAttribute("href")).toEqual("/wiki/recipes/pizza.md");
    expect(screen.getByText("menu.pdf").getAttribute("href")).toEqual(
      "/api/v1/files/wiki/recipes/menu.pdf",
    );
    expect(document.title).toEqual("Recipes");
  });

  it("gives a member the link that starts a new page here", () => {
    show(FOLDER);

    const link = screen.getByText("new page in recipes");
    expect(link.getAttribute("href")).toEqual("/new?folder=recipes");
  });

  it("says the wiki at the root, and links the journal as a feed", () => {
    show({
      ...FOLDER,
      rel: "",
      title: "wiki",
      crumbs: [],
      index_markdown: null,
      folders: [{ title: "journal", url: "/journal" }],
      new_url: "/new",
    });

    expect(document.querySelector("article.j-prose")).toBeNull();
    const journal = screen.getByText("journal");
    expect(journal.getAttribute("href")).toEqual("/journal");
    expect(screen.getByText("new page in the wiki").getAttribute("href")).toEqual("/new");
  });

  it("says there is nothing yet in an empty folder, and nothing to create for a guest", () => {
    show({
      ...FOLDER,
      index_markdown: null,
      folders: [],
      pages: [],
      files: [],
      can_create: false,
    });

    expect(screen.getByText("nothing here yet.")).toBeDefined();
    expect(screen.queryByText("new page in recipes")).toBeNull();
  });
});

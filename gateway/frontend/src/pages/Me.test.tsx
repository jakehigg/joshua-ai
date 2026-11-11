import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Me, { type MePerson } from "./Me";
import type { Session } from "../shell/Shell";
import type { TreeNode } from "../shell/WikiTree";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  links: [],
  timezone: "America/New_York",
};

const ME: MePerson = {
  person: { id: "alex", name: "Alex", role: "member" },
  profile_url: "/wiki/people/alex.md",
  attachments: [{ path: "2026/11/photo.jpg", url: "/attachments/2026/11/photo.jpg" }],
};

const TREE: TreeNode = {
  title: "wiki",
  rel: "",
  url: "/wiki/",
  count: 1,
  children: [{ title: "Home", rel: "Home.md", url: "/wiki/Home.md", count: 0, children: [] }],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function stubRoutes(me: MePerson): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      if (path === "/api/v1/me") {
        return Promise.resolve(json(me));
      }
      if (path === "/api/v1/wiki/tree") {
        return Promise.resolve(json(TREE));
      }
      return Promise.resolve(json(SESSION));
    }),
  );
}

function page() {
  return render(
    <MemoryRouter initialEntries={["/me"]}>
      <Me />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the page of the person", () => {
  it("shows the name, the profile, the journal, the attachments and the tree", async () => {
    stubRoutes(ME);

    page();

    const title = await screen.findByText("Alex");
    expect(title.className).toEqual("j-doc-title");
    expect(screen.getByText("profile").getAttribute("href")).toEqual("/wiki/people/alex.md");
    expect(screen.getByText("journal").getAttribute("href")).toEqual("/journal");
    expect(document.querySelector("p.j-meta")).not.toBeNull();
    expect(screen.getByText("attachments")).toBeDefined();
    expect(screen.getByText("2026/11/photo.jpg").getAttribute("href")).toEqual(
      "/attachments/2026/11/photo.jpg",
    );
    expect(await screen.findByText("Home")).toBeDefined();
    expect(document.title).toEqual("Alex");
  });

  it("says there is nothing yet when the person has no attachment", async () => {
    stubRoutes({ ...ME, attachments: [] });

    page();

    expect(await screen.findByText("attachments")).toBeDefined();
    expect(screen.getByText("nothing yet.")).toBeDefined();
  });

  it("gives the anonymous person no profile link and no attachments", async () => {
    stubRoutes({
      person: { id: "anonymous", name: "anonymous", role: "member" },
      profile_url: null,
      attachments: [],
    });

    page();

    expect(await screen.findByText("journal")).toBeDefined();
    expect(screen.queryByText("profile")).toBeNull();
    expect(screen.queryByText("attachments")).toBeNull();
  });

  it("says what went wrong when the viewer refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "forbidden" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    page();

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
  });
});

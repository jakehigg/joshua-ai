import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Shell, { embedded, type Session } from "./Shell";
import { SECTIONS } from "./sections";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [
    { key: "wiki", label: "Wiki", path: "/wiki/" },
    { key: "journal", label: "Journal", path: "/journal" },
    { key: "usage", label: "Usage", path: "/usage" },
    { key: "chat", label: "Chat", path: "/chat/alex" },
  ],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

function stubSession(session: Session = SESSION) {
  const mock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(session), {
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** A window: `inFrame` makes its `top` another window, as a frame has. */
function view(search: string, inFrame: boolean): Window {
  const fake: Record<string, unknown> = { location: { search } };
  fake["self"] = fake;
  fake["top"] = inFrame ? { location: { search: "" } } : fake;
  return fake as unknown as Window;
}

function shell(section = "me") {
  return render(
    <MemoryRouter>
      <Shell section={section}>
        <p>the body</p>
      </Shell>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the shell", () => {
  it("draws the sidebar, the app bar, the tabs and the body", async () => {
    stubSession();

    shell();

    expect(screen.getByText("the body")).toBeDefined();
    expect(document.querySelector(".j-shell.has-sidebar")).not.toBeNull();
    expect(document.querySelector(".j-sidebar .j-brand")).not.toBeNull();
    expect(document.querySelector(".j-main header.j-appbar")).not.toBeNull();
    expect(document.querySelector("nav.j-tabs.mobile-only")).not.toBeNull();
    expect(document.querySelector("main.j-content")).not.toBeNull();
    expect((await screen.findAllByText("Wiki")).length).toEqual(2);
    expect(document.querySelectorAll("nav.v-sections").length).toEqual(3);
    expect(document.querySelector("nav.v-sections.v-you")).not.toBeNull();
    expect(screen.getByText("Your page")).toBeDefined();
  });

  it("holds the search field of the app bar", () => {
    stubSession();

    shell();

    expect(document.querySelector("header.j-appbar form.v-search")).not.toBeNull();
    expect(document.querySelector('form.v-search input[name="q"]')).not.toBeNull();
  });

  it("gives a migrated section a router link and the rest a plain link", async () => {
    expect(SECTIONS.filter((item) => item.migrated).map((item) => item.key)).toEqual([
      "wiki",
      "journal",
      "search",
      "usage",
      "documents",
    ]);
    stubSession();

    shell();

    const wiki = (await screen.findAllByText("Wiki"))[0];
    expect(wiki?.tagName).toEqual("A");
    expect(wiki?.getAttribute("href")).toEqual("/wiki/");
    const journal = screen.getAllByText("Journal")[0];
    expect(journal?.getAttribute("href")).toEqual("/journal");
    const chat = screen.getAllByText("Chat")[0];
    expect(chat?.getAttribute("href")).toEqual("/chat/alex");
  });

  it("marks the current item, and the wide content of the usage section", async () => {
    stubSession();

    shell("usage");

    expect(document.querySelector("main.j-content.wide")).not.toBeNull();
    const usage = (await screen.findAllByText("Usage"))[0];
    expect(usage?.getAttribute("aria-current")).toEqual("page");
  });

  it("holds the links group of the sidebar and the links tab of a phone", async () => {
    stubSession();

    shell();

    expect(document.querySelector("nav.v-sections.v-links")).not.toBeNull();
    const tab = [...document.querySelectorAll("nav.j-tabs a")].find(
      (item) => item.textContent === "Links",
    );
    expect(tab?.getAttribute("href")).toEqual("/links");
    expect(await screen.findByText("Add links")).toBeDefined();
  });

  it("drops the navigation in a frame, and asks for no session", () => {
    const fetchMock = stubSession();
    window.history.replaceState({}, "", "/documents/alex/party?embed=1");

    try {
      shell("documents");

      expect(screen.getByText("the body")).toBeDefined();
      expect(document.querySelector("main.j-content")).not.toBeNull();
      expect(document.querySelector(".j-shell.has-sidebar")).toBeNull();
      expect(document.querySelector("aside.j-sidebar")).toBeNull();
      expect(document.querySelector("header.j-appbar")).toBeNull();
      expect(document.querySelector("nav.j-tabs")).toBeNull();
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      window.history.replaceState({}, "", "/");
    }
  });

  it("reads a frame of the chat, and ?embed=1, as embedded", () => {
    const blocked = {
      location: { search: "" },
      get self(): unknown {
        return blocked;
      },
      get top(): unknown {
        throw new Error("another origin");
      },
    };

    expect(embedded(view("", false))).toBe(false);
    expect(embedded(view("?rev=abc", false))).toBe(false);
    expect(embedded(view("", true))).toBe(true);
    expect(embedded(view("?embed=1", false))).toBe(true);
    // The chat frames the page of a document, which is of another origin for
    // the frame when it reads `top`.
    expect(embedded(blocked as unknown as Window)).toBe(true);
    // The page of this app, which no frame holds.
    expect(embedded()).toBe(false);
  });

  it("still shows the body when the session does not load", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    shell();

    expect(screen.getByText("the body")).toBeDefined();
    expect(await screen.findByText("Your page")).toBeDefined();
  });
});

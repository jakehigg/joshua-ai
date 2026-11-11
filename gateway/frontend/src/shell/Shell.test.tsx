import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Shell, { type Session } from "./Shell";
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
  links: [{ title: "Weather", url: "https://example.com/weather", icon: "cloud" }],
  timezone: "America/New_York",
};

function stubSession(session: Session = SESSION): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(session), {
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
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

  it("draws the links of the sidebar and the way to edit them", async () => {
    stubSession();

    shell();

    const link = await screen.findByText("Weather");
    expect(link.closest("a")?.getAttribute("href")).toEqual("https://example.com/weather");
    expect(link.closest("a")?.getAttribute("rel")).toEqual("noopener noreferrer");
    expect(screen.getByText("Edit links")).toBeDefined();
  });

  it("asks to add a link when there is none", async () => {
    stubSession({ ...SESSION, links: [] });

    shell();

    expect(await screen.findByText("Add links")).toBeDefined();
  });

  it("still shows the body when the session does not load", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    shell();

    expect(screen.getByText("the body")).toBeDefined();
    expect(await screen.findByText("Your page")).toBeDefined();
  });
});

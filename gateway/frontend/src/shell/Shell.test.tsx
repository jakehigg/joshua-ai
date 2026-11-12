import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
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

/**
 * The session, for every address the shell reads: `/session`, and `/links` of
 * the links group of the sidebar. Each call gets a Response of its own,
 * because a body is read once. With one shared Response, only the first
 * reader gets the session, and the sidebar then draws no section.
 */
function stubSession(session: Session = SESSION) {
  const mock = vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify(session), {
        headers: { "Content-Type": "application/json" },
      }),
    ),
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
  it("draws the sidebar, the app bar and the body", async () => {
    stubSession();

    shell();

    expect(screen.getByText("the body")).toBeDefined();
    expect(document.querySelector(".j-shell.has-sidebar")).not.toBeNull();
    expect(document.querySelector(".j-sidebar .j-brand")).not.toBeNull();
    expect(document.querySelector(".j-main header.j-appbar")).not.toBeNull();
    expect(document.querySelector("nav.j-tabs")).toBeNull();
    expect(document.querySelector("main.j-content")).not.toBeNull();
    expect((await screen.findAllByText("Wiki")).length).toEqual(1);
    expect(document.querySelectorAll("nav.v-sections").length).toEqual(3);
    expect(document.querySelector("nav.v-sections.v-you")).not.toBeNull();
    expect(screen.getByText("Your page")).toBeDefined();
  });

  it("has no search field in the app bar", () => {
    stubSession();

    shell();

    expect(document.querySelector("header.j-appbar")).not.toBeNull();
    expect(document.querySelector("header.j-appbar form")).toBeNull();
    expect(document.querySelector("header.j-appbar input")).toBeNull();
    expect(document.querySelector('input[name="q"]')).toBeNull();
  });

  it("lights up only the current section", async () => {
    stubSession();

    shell("journal");

    await screen.findAllByText("Journal");
    const lit = [...document.querySelectorAll(".j-sidebar [aria-current]")].map((el) => el.textContent);
    expect(lit).toEqual(["Journal"]);
  });

  it("gives a migrated section a router link", async () => {
    expect(SECTIONS.filter((item) => item.migrated).map((item) => item.key)).toEqual([
      "wiki",
      "journal",
      "search",
      "usage",
      "assignments",
      "documents",
      "chat",
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

  it("lets the chat fill the content area", async () => {
    stubSession();

    shell("chat");

    expect(document.querySelector("main.j-content.fill")).not.toBeNull();
    expect(document.querySelector(".j-shell.fill")).not.toBeNull();
    await screen.findAllByText("Wiki");
  });

  it("marks the current item, and the wide content of the usage section", async () => {
    stubSession();

    shell("usage");

    expect(document.querySelector("main.j-content.wide")).not.toBeNull();
    // The item of the sidebar, which the session draws. The app bar names the
    // section too, and that title is no item of the navigation.
    const sidebar = document.querySelector("aside.j-sidebar") as HTMLElement;
    const usage = await within(sidebar).findByText("Usage");
    expect(usage.getAttribute("aria-current")).toEqual("page");
  });

  it("holds the links group of the sidebar", async () => {
    stubSession();

    shell();

    expect(document.querySelector("nav.v-sections.v-links")).not.toBeNull();
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

function Go() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/usage")}>
      go
    </button>
  );
}

describe("the navigation drawer", () => {
  function drawer() {
    stubSession();
    render(
      <MemoryRouter>
        <Shell section="me">
          <Go />
        </Shell>
      </MemoryRouter>,
    );
    return screen.getByRole("button", { name: "Show the navigation" });
  }
  const isOpen = () => document.querySelector(".j-sidebar.open") !== null;

  it("opens from the hamburger, with its aria state, focus and a locked page", () => {
    const toggle = drawer();
    expect(toggle.getAttribute("aria-controls")).toBe("app-nav");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(isOpen()).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close the navigation" }));
    expect(document.documentElement.classList.contains("j-lock")).toBe(true);
  });

  it("closes on the backdrop, the close button and Escape, and gives the focus back", () => {
    const toggle = drawer();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByTestId("nav-backdrop"));
    expect(isOpen()).toBe(false);
    expect(document.activeElement).toBe(toggle);
    expect(document.documentElement.classList.contains("j-lock")).toBe(false);

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Close the navigation" }));
    expect(isOpen()).toBe(false);

    fireEvent.click(toggle);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(isOpen()).toBe(false);
  });

  it("closes on a click on a link and on a route change", async () => {
    const toggle = drawer();
    fireEvent.click(toggle);
    fireEvent.click(await screen.findByText("Journal"));
    expect(isOpen()).toBe(false);

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    expect(isOpen()).toBe(false);
  });

  it("has no hamburger in the chat, where the rail is the menu", () => {
    stubSession();
    render(
      <MemoryRouter>
        <Shell section="chat">
          <p>chat</p>
        </Shell>
      </MemoryRouter>,
    );
    expect(screen.queryByRole("button", { name: "Show the navigation" })).toBeNull();
  });
});

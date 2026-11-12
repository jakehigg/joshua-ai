import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceItem } from "../api/workspaces";
import { reloadPanels, STORAGE_KEY } from "../shell/panels";
import { WorkspacePage } from "./ChatPage";

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const WS = {
  id: "w1", slug: "solar", title: "Solar install", goal: "", notes: "", servers: [],
  model: null, notify: "none", status: "active",
};

function item(over: Partial<WorkspaceItem>): WorkspaceItem {
  return {
    id: "i1", kind: "link", ref: "r", title: "T", url: null, added_by: "person",
    pinned: false, seen: true, ...over,
  };
}

const ITEMS = [
  item({ id: "a", kind: "document", ref: "documents/jake/smoke-test-note", title: "Smoke note", url: "/documents/jake/smoke-test-note" }),
  item({ id: "b", kind: "wiki", ref: "w", title: "Menu notes", url: "/wiki/menu.md" }),
  item({ id: "c", kind: "pr", ref: "p", title: "Fix the thing", url: "https://github.com/a/b/pull/1" }),
  item({ id: "d", kind: "tasks_project", ref: "trip-plan", title: "Trip plan" }),
];

function backend() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "http://test").pathname;
      if (path === "/api/v1/session") return json({ person: { id: "alex", name: "Alex", role: "member" }, sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }] });
      if (path === "/api/v1/workspaces/solar") return json(WS);
      if (path === "/api/v1/workspaces") return json({ workspaces: [WS] });
      if (path === "/api/v1/workspaces/solar/items") return json({ items: ITEMS });
      if (path.endsWith("/history")) return json({ messages: [], nextBefore: null });
      return json({ error: "not found" }, 404);
    }),
  );
}

async function open() {
  backend();
  render(
    <MemoryRouter initialEntries={["/chat/w/solar"]} future={FUTURE}>
      <Routes>
        <Route path="/chat/w/:slug" element={<WorkspacePage />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Solar install" });
}

const screenEl = () => document.querySelector(".chat-screen") as HTMLElement;
const frame = () => document.querySelector(".doc-panel-frame") as HTMLIFrameElement | null;

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  reloadPanels();
});

describe("sheets on the chat screen", () => {
  it("opens the items, and the backdrop, the close button and Escape close them", async () => {
    await open();
    const itemsButton = document.querySelector(".ws-header-actions button") as HTMLButtonElement;
    fireEvent.click(itemsButton);
    expect(document.querySelector(".items.open")).toBeTruthy();
    expect(screenEl().className).toContain("sheet-open");
    fireEvent.click(screen.getByTestId("items-backdrop"));
    expect(document.querySelector(".items.open")).toBeNull();
    expect(screenEl().className).not.toContain("sheet-open");

    fireEvent.click(itemsButton);
    fireEvent.click(screen.getByRole("button", { name: "Close the items", hidden: true }));
    expect(document.querySelector(".items.open")).toBeNull();

    fireEvent.click(itemsButton);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".items.open")).toBeNull();
  });

  it("closes the rail on the backdrop, the close button, Escape and a pick", async () => {
    await open();
    const burger = screen.getByRole("button", { name: "Show the workspaces", hidden: true });
    fireEvent.click(burger);
    expect(document.querySelector(".rail.open")).toBeTruthy();
    fireEvent.click(screen.getByTestId("rail-backdrop"));
    expect(document.querySelector(".rail.open")).toBeNull();

    fireEvent.click(burger);
    fireEvent.click(screen.getByRole("button", { name: "Close the workspaces", hidden: true }));
    expect(document.querySelector(".rail.open")).toBeNull();

    fireEvent.click(burger);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".rail.open")).toBeNull();

    fireEvent.click(burger);
    fireEvent.click(screen.getByRole("button", { name: "+ New workspace", hidden: true }));
    expect(document.querySelector(".rail.open")).toBeNull();

    fireEvent.click(burger);
    fireEvent.click(screen.getByRole("link", { name: "Chat", hidden: true }));
    expect(document.querySelector(".rail.open")).toBeNull();
  });

  it("has one toggle on a phone, and its drawer holds the workspaces and the app links", async () => {
    await open();
    expect(screen.queryByRole("button", { name: "Show the navigation" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show the workspaces", hidden: true }));
    const rail = document.querySelector(".rail.open") as HTMLElement;
    expect(within(rail).getByRole("link", { name: "Solar install", hidden: true })).toBeTruthy();
    expect(within(rail).getByRole("button", { name: "+ New workspace", hidden: true })).toBeTruthy();
    expect(await within(rail).findByRole("link", { name: "Wiki", hidden: true })).toBeTruthy();
    expect(within(rail).getByRole("link", { name: "Your page", hidden: true })).toBeTruthy();
    // Chat is the first entry of the rail, so the app links do not repeat it,
    // and only the thread on screen is lit.
    expect(within(rail).getAllByRole("link", { name: "Chat", hidden: true })).toHaveLength(1);
    const lit = [...rail.querySelectorAll("[aria-current]")].map((el) => el.textContent);
    expect(lit).toEqual(["Solar install"]);
    // The drawer takes the focus on its close button, not on a link.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close the workspaces", hidden: true }));
    // A tap on an app link closes the drawer.
    fireEvent.click(within(rail).getByRole("link", { name: "Wiki", hidden: true }));
    expect(document.querySelector(".rail.open")).toBeNull();
  });

  it("has a single top bar on a phone: the menu, the title and the items button", async () => {
    await open();
    const header = document.querySelector(".chat-header") as HTMLElement;
    expect(within(header).getByRole("button", { name: "Show the workspaces", hidden: true })).toBeTruthy();
    expect(within(header).getByRole("heading", { name: "Solar install" })).toBeTruthy();
    expect(within(header).getByRole("button", { name: "Items" })).toBeTruthy();
    // The app bar is drawn, and hidden by CSS on a phone; it holds no menu
    // button and no search of its own on this page.
    expect(screen.queryByRole("button", { name: "Show the navigation" })).toBeNull();
    expect(document.querySelector(".j-appbar input")).toBeNull();
    expect(document.querySelector(".j-shell.fill")).not.toBeNull();
  });

  it("keeps one sheet open: the rail closes the items", async () => {
    await open();
    fireEvent.click(document.querySelector(".ws-header-actions button") as HTMLButtonElement);
    fireEvent.click(screen.getByRole("button", { name: "Show the workspaces", hidden: true }));
    expect(document.querySelector(".items.open")).toBeNull();
    expect(document.querySelector(".rail.open")).toBeTruthy();
  });

  it("opens a document item in the panel, closes the items, and Escape closes the panel", async () => {
    await open();
    fireEvent.click(document.querySelector(".ws-header-actions button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("button", { name: "Smoke note", hidden: true }));
    expect(frame()?.getAttribute("src")).toBe("/documents/jake/smoke-test-note");
    expect(document.querySelector(".items.open")).toBeNull();
    expect(frame()?.hasAttribute("sandbox")).toBe(false);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".doc-panel")).toBeNull();
    expect(screenEl().className).not.toContain("sheet-open");
  });

  it("frames a viewer page of an item", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Menu notes", hidden: true }));
    expect(frame()?.getAttribute("src")).toBe("/wiki/menu.md");
    fireEvent.click(screen.getByRole("button", { name: "Close the item" }));
    expect(document.querySelector(".doc-panel")).toBeNull();
  });

  it("shows a card for an external link, with a new-tab button and no navigation", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Fix the thing", hidden: true }));
    expect(frame()).toBeNull();
    const panel = document.querySelector(".doc-panel") as HTMLElement;
    expect(panel.textContent).toContain("Pull request");
    expect(panel.textContent).toContain("https://github.com/a/b/pull/1");
    const link = screen.getByRole("link", { name: "Open in new tab" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("shows a card for an item with no url", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Trip plan", hidden: true }));
    const panel = document.querySelector(".doc-panel") as HTMLElement;
    expect(panel.textContent).toContain("Task project");
    expect(panel.textContent).toContain("trip-plan");
    expect(screen.queryByRole("link", { name: "Open in new tab" })).toBeNull();
    await act(async () => undefined);
  });
});

describe("the side columns on a desktop", () => {
  const rail = () => document.querySelector(".rail") as HTMLElement;
  const items = () => document.querySelector(".items") as HTMLElement;
  const shell = () => document.querySelector(".j-shell") as HTMLElement;
  const click = (name: string) =>
    fireEvent.click(screen.getByRole("button", { name, hidden: true }));

  it("folds and opens the rail, the items and the sidebar with their buttons", async () => {
    await open();
    expect(rail().className).not.toContain("collapsed");
    click("Hide the workspaces");
    expect(rail().className).toContain("collapsed");
    expect(screen.getByRole("button", { name: "Open the workspaces column", hidden: true })).toBeTruthy();
    fireEvent.click(document.querySelector(".rail-expand") as HTMLButtonElement);
    expect(rail().className).not.toContain("collapsed");

    click("Hide the items");
    expect(items().className).toContain("collapsed");
    expect(screenEl().className).toContain("items-collapsed");
    fireEvent.click(document.querySelector(".items-toggle") as HTMLButtonElement);
    expect(items().className).not.toContain("collapsed");

    click("Hide the navigation");
    expect(shell().className).toContain("nav-collapsed");
    fireEvent.click(document.querySelector(".chat-header .nav-expand") as HTMLButtonElement);
    expect(shell().className).not.toContain("nav-collapsed");
  });

  it("has aria state on the buttons", async () => {
    await open();
    const hide = screen.getByRole("button", { name: "Hide the workspaces", hidden: true });
    expect(hide.getAttribute("aria-expanded")).toBe("true");
    expect(hide.getAttribute("aria-controls")).toBe("chat-rail");
    fireEvent.click(hide);
    expect(hide.getAttribute("aria-expanded")).toBe("false");
  });

  it("focus mode folds everything, and a second press opens everything", async () => {
    await open();
    const focus = screen.getByRole("button", { name: "Focus mode", hidden: true });
    fireEvent.click(focus);
    expect(shell().className).toContain("nav-collapsed");
    expect(rail().className).toContain("collapsed");
    expect(items().className).toContain("collapsed");
    expect(focus.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(focus);
    expect(shell().className).not.toContain("nav-collapsed");
    expect(rail().className).not.toContain("collapsed");
    expect(items().className).not.toContain("collapsed");
    // The keyboard shortcut does the same.
    fireEvent.keyDown(document, { key: "\\", ctrlKey: true });
    expect(rail().className).toContain("collapsed");
  });

  it("keeps the state in localStorage and restores it", async () => {
    await open();
    click("Hide the workspaces");
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}")).toMatchObject({ rail: true });
    cleanup();
    reloadPanels();
    await open();
    expect(rail().className).toContain("collapsed");
    expect(items().className).not.toContain("collapsed");
  });

  it("restores a stored state, and works with no storage", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ nav: true, items: true }));
    reloadPanels();
    await open();
    expect(shell().className).toContain("nav-collapsed");
    expect(items().className).toContain("collapsed");
    expect(rail().className).not.toContain("collapsed");
    cleanup();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("no storage");
    });
    reloadPanels();
    await open();
    click("Hide the workspaces");
    expect(rail().className).toContain("collapsed");
    vi.restoreAllMocks();
  });

  it("leaves the phone drawers alone: the drawer still opens when a column is folded", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ rail: true }));
    reloadPanels();
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Show the workspaces", hidden: true }));
    expect(document.querySelector(".rail.open")).toBeTruthy();
    fireEvent.click(screen.getByTestId("rail-backdrop"));
    expect(document.querySelector(".rail.open")).toBeNull();
  });
});

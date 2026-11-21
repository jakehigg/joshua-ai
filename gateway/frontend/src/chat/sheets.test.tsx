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
  id: "w1", slug: "solar", title: "Solar install", owner: "alex", goal: "", notes: "", servers: [],
  model: null, notify: "none", status: "active", shared: false, members: [],
};

function item(over: Partial<WorkspaceItem>): WorkspaceItem {
  return {
    id: "i1", kind: "link", ref: "r", title: "T", url: null, added_by: "person",
    pinned: false, seen: true, ...over,
  };
}

const ITEMS = [
  item({ id: "a", kind: "document", ref: "documents/alex/garden-plan", title: "Garden plan", url: "/documents/alex/garden-plan" }),
  item({ id: "h", kind: "artifact", ref: "documents/alex/garden-plan-mockup.html", title: "Mockup", url: "/documents/alex/garden-plan-mockup" }),
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
      if (path === "/api/v1/workspaces/alex%2Fsolar") return json(WS);
      if (path === "/api/v1/workspaces") return json({ workspaces: [WS] });
      // The screen asks for the items by the id of the workspace, never by
      // the ref of the address it was opened at.
      if (path === "/api/v1/workspaces/w1/items") return json({ items: ITEMS });
      if (path.endsWith("/history")) return json({ messages: [], nextBefore: null });
      return json({ error: "not found" }, 404);
    }),
  );
}

async function open() {
  backend();
  render(
    <MemoryRouter initialEntries={["/chat/w/alex/solar"]} future={FUTURE}>
      <Routes>
        <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
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

  it("opens the one navigation drawer from the chat header, and a link closes it", async () => {
    await open();
    const burger = screen.getByRole("button", { name: "Show the navigation", hidden: true });
    fireEvent.click(burger);
    const drawer = document.querySelector(".j-sidebar.open") as HTMLElement;
    expect(await within(drawer).findByRole("link", { name: "Wiki", hidden: true })).toBeTruthy();
    expect(within(drawer).getByRole("link", { name: "Profile", hidden: true })).toBeTruthy();
    // The drawer takes the focus on its close button, not on a link.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close the navigation", hidden: true }),
    );
    // Escape closes it, and the hamburger of the chat header takes the focus
    // back, though it is no child of the shell.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".j-sidebar.open")).toBeNull();
    expect(document.activeElement).toBe(burger);

    // A tap on a link of the drawer closes it too.
    fireEvent.click(burger);
    fireEvent.click(within(drawer).getByRole("link", { name: "Wiki", hidden: true }));
    expect(document.querySelector(".j-sidebar.open")).toBeNull();
  });

  it("has a single top bar on a phone: the menu, the title and the items button", async () => {
    await open();
    const header = document.querySelector(".chat-header") as HTMLElement;
    expect(within(header).getByRole("button", { name: "Show the navigation", hidden: true })).toBeTruthy();
    expect(within(header).getByRole("heading", { name: "Solar install" })).toBeTruthy();
    expect(within(header).getByRole("button", { name: "Items" })).toBeTruthy();
    // The app bar is drawn, and hidden by CSS on a phone; it holds no menu
    // button and no search of its own on this page.
    expect(document.querySelector(".j-appbar .j-nav-toggle")).toBeNull();
    expect(document.querySelector(".j-appbar input")).toBeNull();
    expect(document.querySelector(".j-shell.fill")).not.toBeNull();
    // The chat screen draws no list of workspaces of its own.
    expect(document.querySelector(".chat-screen .v-chats")).toBeNull();
  });

  it("opens a document item in the panel, closes the items, and Escape closes the panel", async () => {
    await open();
    fireEvent.click(document.querySelector(".ws-header-actions button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("button", { name: "Garden plan", hidden: true }));
    expect(frame()?.getAttribute("src")).toBe("/documents/alex/garden-plan?embed=1");
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
  const items = () => document.querySelector(".items") as HTMLElement;
  const shell = () => document.querySelector(".j-shell") as HTMLElement;
  const click = (name: string) =>
    fireEvent.click(screen.getByRole("button", { name, hidden: true }));

  it("folds and opens the items and the sidebar with their buttons", async () => {
    await open();
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
    const hide = screen.getByRole("button", { name: "Hide the items", hidden: true });
    expect(hide.getAttribute("aria-expanded")).toBe("true");
    expect(hide.getAttribute("aria-controls")).toBe("workspace-items");
    fireEvent.click(hide);
    expect(hide.getAttribute("aria-expanded")).toBe("false");
  });

  it("focus mode folds everything, and a second press opens everything", async () => {
    await open();
    const focus = screen.getByRole("button", { name: "Focus mode", hidden: true });
    fireEvent.click(focus);
    expect(shell().className).toContain("nav-collapsed");
    expect(items().className).toContain("collapsed");
    expect(focus.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(focus);
    expect(shell().className).not.toContain("nav-collapsed");
    expect(items().className).not.toContain("collapsed");
    // The keyboard shortcut does the same.
    fireEvent.keyDown(document, { key: "\\", ctrlKey: true });
    expect(items().className).toContain("collapsed");
  });

  it("keeps the state in localStorage and restores it", async () => {
    await open();
    click("Hide the items");
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}")).toMatchObject({ items: true });
    cleanup();
    reloadPanels();
    await open();
    expect(items().className).toContain("collapsed");
    expect(shell().className).not.toContain("nav-collapsed");
  });

  it("restores a stored state, and works with no storage", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ nav: true, items: true }));
    reloadPanels();
    await open();
    expect(shell().className).toContain("nav-collapsed");
    expect(items().className).toContain("collapsed");
    cleanup();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("no storage");
    });
    reloadPanels();
    await open();
    click("Hide the items");
    expect(items().className).toContain("collapsed");
    vi.restoreAllMocks();
  });

  it("leaves the phone drawer alone: the item drawer still opens when the column is folded", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ items: true }));
    reloadPanels();
    await open();
    fireEvent.click(document.querySelector(".ws-header-actions button") as HTMLButtonElement);
    expect(document.querySelector(".items.open")).toBeTruthy();
    fireEvent.click(screen.getByTestId("items-backdrop"));
    expect(document.querySelector(".items.open")).toBeNull();
  });
});

describe("the item column and the panel", () => {
  const items = () => document.querySelector(".items") as HTMLElement;

  it("steps aside while the panel is open, keeps the saved fold, and comes back", async () => {
    await open();
    expect(items().className).not.toContain("collapsed");
    fireEvent.click(await screen.findByRole("button", { name: "Garden plan", hidden: true }));
    expect(items().className).toContain("collapsed");
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}").items ?? false).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    expect(items().className).not.toContain("collapsed");
  });

  it("closes the panel when Items is opened on purpose", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Garden plan", hidden: true }));
    fireEvent.click(document.querySelector(".items-toggle") as HTMLButtonElement);
    expect(document.querySelector(".doc-panel")).toBeNull();
    expect(items().className).not.toContain("collapsed");
  });

  it("opens an .html artifact with its real path", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Mockup", hidden: true }));
    expect(frame()?.getAttribute("src")).toBe("/documents/alex/garden-plan-mockup?embed=1");
  });
});

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
  item({ id: "e", kind: "file", ref: "wiki/photo.jpg", title: "Photo", url: "/api/v1/files/wiki/photo.jpg" }),
  item({ id: "f", kind: "file", ref: "wiki/plan.pdf", title: "Plan", url: "/api/v1/files/wiki/plan.pdf" }),
  item({ id: "g", kind: "task", ref: "t-7", title: "Call the installer", url: "https://tasks.test/t-7" }),
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

  it("serves every kind of item in the one sheet", async () => {
    await open();
    const show = async (name: string) =>
      fireEvent.click(await screen.findByRole("button", { name, hidden: true }));
    const close = () => fireEvent.keyDown(document, { key: "Escape" });

    // A document and an HTML artifact: the document page of the viewer, which
    // is where the sandbox of an artifact lives.
    await show("Garden plan");
    expect(frame()?.getAttribute("src")).toBe("/documents/alex/garden-plan?embed=1");
    close();
    await show("Mockup");
    expect(frame()?.getAttribute("src")).toBe("/documents/alex/garden-plan-mockup?embed=1");
    close();

    // A wiki page, and a PDF the viewer serves: both are framed.
    await show("Menu notes");
    expect(frame()?.getAttribute("src")).toBe("/wiki/menu.md");
    close();
    await show("Plan");
    expect(frame()?.getAttribute("src")).toBe("/api/v1/files/wiki/plan.pdf");
    close();

    // A photo, full size in the sheet.
    await show("Photo");
    expect(screen.getByAltText("Photo").getAttribute("src")).toBe("/api/v1/files/wiki/photo.jpg");
    close();

    // A task and a link of another site: a card with the address.
    await show("Call the installer");
    expect((document.querySelector(".doc-panel") as HTMLElement).textContent).toContain("Task");
    expect(screen.getByRole("link", { name: "Open in new tab" }).getAttribute("href")).toBe(
      "https://tasks.test/t-7",
    );
    close();
    await show("Fix the thing");
    expect(screen.getByRole("link", { name: "Open in new tab" }).getAttribute("href")).toBe(
      "https://github.com/a/b/pull/1",
    );
    close();
    await act(async () => undefined);
  });

  it("keeps the thread and what is typed in it while an item is open", async () => {
    await open();
    const thread = document.querySelector(".list") as HTMLElement;
    const composer = screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: "half a question" } });
    fireEvent.click(await screen.findByRole("button", { name: "Garden plan", hidden: true }));
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    // The conversation stayed mounted behind the sheet, so it keeps where it
    // was scrolled to and what was typed.
    expect(document.querySelector(".list")).toBe(thread);
    expect((screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement).value).toBe(
      "half a question",
    );
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

  it("keeps its place while the sheet is open, and the saved fold with it", async () => {
    await open();
    expect(items().className).not.toContain("collapsed");
    fireEvent.click(await screen.findByRole("button", { name: "Garden plan", hidden: true }));
    // The item stands over the row, so the column stays and the conversation
    // keeps its width.
    expect(items().className).not.toContain("collapsed");
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}").items ?? false).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    expect(document.querySelector(".item-sheet")).toBeNull();
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

describe("the item sheet of a workspace", () => {
  it("opens over the workspace as a dialog, and the dim area closes it", async () => {
    await open();
    const row = await screen.findByRole("button", { name: "Garden plan", hidden: true });
    // A tap on a row gives it the focus, as a browser does.
    row.focus();
    fireEvent.click(row);
    const box = document.querySelector(".item-sheet-box") as HTMLElement;
    expect(box.getAttribute("role")).toBe("dialog");
    expect(box.getAttribute("aria-modal")).toBe("true");
    expect(box.getAttribute("aria-label")).toBe("garden-plan");
    // The workspace is behind it, dimmed and still mounted.
    expect(document.querySelector(".chat .chat-header")).not.toBeNull();
    expect(frame()?.getAttribute("src")).toBe("/documents/alex/garden-plan?embed=1");
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close the document" }),
    );
    fireEvent.click(screen.getByTestId("sheet-backdrop"));
    expect(document.querySelector(".item-sheet")).toBeNull();
    // The row that opened the item has the focus back.
    expect(document.activeElement).toBe(row);
  });

  it("goes full screen from the header of the sheet, and back", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Menu notes", hidden: true }));
    const box = () => document.querySelector(".item-sheet-box") as HTMLElement;
    expect(box().className).not.toContain("full");
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(box().className).toContain("full");
    const back = screen.getByRole("button", { name: "Leave full screen" });
    expect(back.getAttribute("aria-pressed")).toBe("true");
    // The bar is still there in full screen, with the title and every control.
    const bar = box().querySelector(".doc-panel-header") as HTMLElement;
    expect(bar.querySelector(".doc-panel-title")?.textContent).toBe("Menu notes");
    expect(bar.contains(back)).toBe(true);
    expect(bar.querySelector(".doc-panel-full")).not.toBeNull();
    expect(bar.querySelector(".doc-panel-close")).not.toBeNull();
    // The window is the whole sheet now: there is no edge to drag.
    expect(document.querySelector(".item-sheet-box .panel-divider")).toBeNull();
    fireEvent.click(back);
    expect(box().className).not.toContain("full");
  });

  it("holds the Tab key inside the sheet", async () => {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Menu notes", hidden: true }));
    // The bar of the sheet: the title, the control that opens the item on its
    // own page, the full-screen control, and the X.
    const first = screen.getByRole("link", { name: "Open full page" });
    const last = screen.getByRole("separator", { name: "Resize the item" });
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });
});

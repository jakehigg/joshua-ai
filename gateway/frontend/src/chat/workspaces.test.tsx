import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Workspace, WorkspaceItem } from "../api/workspaces";
import { NavContext } from "../shell/AppNav";
import { SessionContext, type Session } from "../shell/Shell";
import { ChatPage, WorkspaceLink, WorkspacePage } from "./ChatPage";
import { DocumentPanelProvider, useDocumentPanelState } from "./DocumentPanel";
import { GROUPS_KEY, ItemPanel, UNDO_MS } from "./ItemPanel";
import { NewWorkspace } from "./NewWorkspace";
import { Rail } from "./Rail";
import { validPerson } from "./people";
import { WorkspaceHeader } from "./WorkspaceHeader";

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function ws(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w1",
    slug: "solar",
    title: "Solar install",
    owner: "alex",
    goal: "Pick an installer",
    notes: "",
    servers: ["tasks"],
    model: null,
    notify: "none",
    status: "active",
    shared: false,
    members: [],
    ...over,
  };
}

function session(id: string, name: string): Session {
  return {
    person: { id, name, role: "member" },
    anonymous: false,
    sections: [],
    chat_url: `/chat/${id}`,
    timezone: "America/New_York",
  };
}

function item(over: Partial<WorkspaceItem> = {}): WorkspaceItem {
  return {
    id: "i1",
    kind: "link",
    ref: "https://example.com",
    title: "Quote",
    url: "https://example.com",
    added_by: "person",
    pinned: false,
    seen: true,
    ...over,
  };
}

const OPTIONS = {
  servers: [
    { name: "tasks", description: "Tasks and projects" },
    { name: "github", description: "github" },
  ],
  models: ["model-a", "model-b"],
  // The people the caller may share a workspace with; the caller is not here.
  people: [{ id: "mia", name: "Mia" }],
};

type Call = { method: string; path: string; body: unknown };

// A fake viewer for the workspace routes. `routes` maps "METHOD /path" to a body.
function viewer(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://test");
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path: url.pathname, body });
    const found = routes[`${method} ${url.pathname}`];
    if (found === undefined) return json({ error: "not found" }, 404);
    return json(found);
  });
  vi.stubGlobal("fetch", fetch);
  return { calls, fetch };
}

afterEach(() => vi.unstubAllGlobals());

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

describe("the rail", () => {
  const list = [
    ws({ id: "a", slug: "solar", title: "Solar install" }),
    ws({ id: "b", slug: "trip", title: "Trip", status: "paused" }),
    ws({ id: "c", slug: "old", title: "Old", status: "archived" }),
  ];
  function show(activeSlug?: string, onNew = vi.fn()) {
    render(
      <MemoryRouter future={FUTURE}>
        <Rail person="alex" workspaces={list} activeSlug={activeSlug} open onNew={onNew} onNavigate={vi.fn()} onClose={vi.fn()} />
      </MemoryRouter>,
    );
    return onNew;
  }

  it("lists Chat, the active workspaces and the paused ones in a fold", () => {
    show();
    expect(screen.getByRole("link", { name: "Chat" }).getAttribute("href")).toBe("/chat/alex");
    expect(screen.getByRole("link", { name: "Solar install" }).getAttribute("href")).toBe("/chat/w/alex/solar");
    expect(screen.queryByText("Old")).toBeNull();
    const fold = screen.getByText("Paused (1)").closest("details") as HTMLDetailsElement;
    expect(fold.open).toBe(false);
    expect(within(fold).getByRole("link", { name: "Trip", hidden: true })).toBeTruthy();
  });

  it("marks the page on screen", () => {
    show("solar");
    expect(screen.getByRole("link", { name: "Solar install" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("link", { name: "Chat" }).getAttribute("aria-current")).toBeNull();
  });

  it("marks a shared workspace with the two-person icon and links it under the shared namespace", () => {
    render(
      <MemoryRouter future={FUTURE}>
        <Rail
          person="mia"
          workspaces={[ws({ id: "s", slug: "thanksgiving", title: "Thanksgiving", owner: "alex", shared: true, members: ["mia"] })]}
          open
          onNew={vi.fn()}
          onNavigate={vi.fn()}
          onClose={vi.fn()}
        />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: /Thanksgiving/ });
    expect(link.getAttribute("href")).toBe("/chat/w/shared/thanksgiving");
    // The icon, and no label: "Shared" is nowhere in the row.
    expect(link.querySelector(".shared-mark svg.j-icon")).not.toBeNull();
    expect(link.textContent).toBe("Thanksgiving");
  });

  it("marks no personal workspace", () => {
    show();
    const link = screen.getByRole("link", { name: "Solar install" });
    expect(link.querySelector(".shared-mark")).toBeNull();
    expect(document.querySelector(".shared-mark")).toBeNull();
  });

  it("counts the rows a person has not read, after the name", () => {
    render(
      <MemoryRouter future={FUTURE}>
        <Rail
          person="alex"
          workspaces={[ws({ unread: 3 }), ws({ id: "b", slug: "trip", title: "Trip" })]}
          open
          onNew={vi.fn()}
          onNavigate={vi.fn()}
          onClose={vi.fn()}
        />
      </MemoryRouter>,
    );
    const counted = screen.getByRole("link", { name: /Solar install/ });
    expect(counted.querySelector(".rail-unread")?.textContent).toBe("3");
    // A thread with nothing unread carries no count at all.
    expect(screen.getByRole("link", { name: "Trip" }).querySelector(".rail-unread")).toBeNull();
  });

  it("gives each person of a shared workspace their own count", () => {
    const thread = ws({ slug: "thanksgiving", title: "Thanksgiving", shared: true, members: ["mia"] });
    const rail = (person: string, unread: number) => (
      <MemoryRouter future={FUTURE}>
        <Rail
          person={person}
          workspaces={[{ ...thread, unread }]}
          open
          onNew={vi.fn()}
          onNavigate={vi.fn()}
          onClose={vi.fn()}
        />
      </MemoryRouter>
    );
    const alex = render(rail("alex", 0));
    expect(document.querySelector(".rail-unread")).toBeNull();
    alex.unmount();
    render(rail("mia", 2));
    expect(document.querySelector(".rail-unread")?.textContent).toBe("2");
  });

  it("opens the dialog from the new workspace button", () => {
    const onNew = show();
    fireEvent.click(screen.getByRole("button", { name: "+ New workspace" }));
    expect(onNew).toHaveBeenCalled();
  });

  // The chat page draws no app bar, so on a phone this drawer is the only
  // place the Reload item of the app navigation can be reached.
  it("repeats the app navigation, with its Reload item", () => {
    viewer({});
    render(
      <MemoryRouter future={FUTURE}>
        <NavContext.Provider value={{ sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }], section: "chat" }}>
          <Rail person="alex" workspaces={[]} open onNew={vi.fn()} onNavigate={vi.fn()} onClose={vi.fn()} />
        </NavContext.Provider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Wiki" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  });
});

describe("the new workspace dialog", () => {
  it("creates a workspace with the picked model and servers, then hands it on", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "POST /api/v1/workspaces": ws({ slug: "thanksgiving", title: "Thanksgiving" }),
    });
    const onCreated = vi.fn();
    render(<NewWorkspace onClose={vi.fn()} onCreated={onCreated} />);
    await screen.findByText("Tasks and projects");

    const create = screen.getByRole("button", { name: "Create" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true); // a title is required
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: " Thanksgiving " } });
    fireEvent.change(screen.getByLabelText("Goal (optional)"), { target: { value: "A meal for 8" } });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-b" } });
    fireEvent.click(screen.getByLabelText(/tasks/));
    fireEvent.click(create);

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({
      title: "Thanksgiving",
      goal: "A meal for 8",
      model: "model-b",
      servers: ["tasks"],
    });
  });

  it("shows the refusal and stays open", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    const onCreated = vi.fn();
    render(<NewWorkspace onClose={vi.fn()} onCreated={onCreated} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "X" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect((await screen.findByRole("alert")).textContent).toContain("not found");
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("shares a workspace with the people picked, and shows the address it takes", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "POST /api/v1/workspaces": ws({ slug: "thanksgiving", title: "Thanksgiving", shared: true, members: ["mia"] }),
    });
    const onCreated = vi.fn();
    render(<NewWorkspace onClose={vi.fn()} onCreated={onCreated} />);
    await screen.findByText("Tasks and projects");

    // The people and the address show only when the workspace is shared.
    expect(screen.queryByLabelText("Mia")).toBeNull();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Thanksgiving" } });
    fireEvent.click(screen.getByLabelText("Shared"));
    expect(screen.getByText(/\/chat\/w\/shared\/thanksgiving/)).toBeTruthy();

    // Shared with nobody is not a workspace: Create waits for a person.
    const create = screen.getByRole("button", { name: "Create" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("Mia"));
    expect(create.disabled).toBe(false);
    fireEvent.click(create);

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      title: "Thanksgiving",
      servers: [],
      members: ["mia"],
    });
  });

  it("says that the slug of a shared workspace is taken, and asks for another title", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://test");
        if ((init?.method ?? "GET") === "POST") {
          return json({ error: "the slug thanksgiving is taken", slug: "thanksgiving" }, 409);
        }
        return url.pathname.endsWith("/options") ? json(OPTIONS) : json({ error: "not found" }, 404);
      }),
    );
    const onCreated = vi.fn();
    render(<NewWorkspace onClose={vi.fn()} onCreated={onCreated} />);
    await screen.findByText("Tasks and projects");
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Thanksgiving" } });
    fireEvent.click(screen.getByLabelText("Shared"));
    fireEvent.click(screen.getByLabelText("Mia"));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "A shared workspace called thanksgiving exists. Pick another title.",
    );
    expect(onCreated).not.toHaveBeenCalled();
  });
});

function Panel({ refreshKey = 0 }: { refreshKey?: number }) {
  const state = useDocumentPanelState();
  return (
    <MemoryRouter future={FUTURE}>
      <DocumentPanelProvider panel={state}>
        <ItemPanel slug="solar" refreshKey={refreshKey} open onClose={vi.fn()} />
        <output data-testid="shown">{state.shown?.url ?? ""}</output>
      </DocumentPanelProvider>
    </MemoryRouter>
  );
}

describe("the item panel", () => {
  const items = [
    item(),
    item({ id: "i2", kind: "document", ref: "documents/alex/pie.md", title: "Pie", url: "/documents/alex/pie" }),
    item({ id: "i3", kind: "link", ref: "x", title: "Sneaky", url: "javascript:alert(1)" }),
    item({ id: "i4", kind: "wiki", ref: "w", title: "Menu notes", url: "/wiki/menu.md", pinned: true }),
  ];

  it("groups by kind and opens items in the panel, never as links", async () => {
    viewer({ "GET /api/v1/workspaces/solar/items": { items } });
    render(<Panel />);
    await screen.findByText("Quote");
    expect(screen.getByText("Links")).toBeTruthy();
    expect(screen.getByText("Documents")).toBeTruthy();
    // No item is a link: nothing here can navigate the chat tab.
    expect(document.querySelector(".items a")).toBeNull();
    expect(screen.getByRole("button", { name: "Quote" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Menu notes" })).toBeTruthy();
    // A javascript: address is text, never a button or an href.
    expect(screen.getByText("Sneaky").closest("button")).toBeNull();
    expect(document.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("shows a photo item as a thumbnail, not the kind icon, and a PDF with the icon", async () => {
    const photo = item({
      id: "p1",
      kind: "file",
      ref: "wiki/attachments/2026/11/garden.jpg",
      title: "An e-paper display",
      url: "/api/v1/files/wiki/attachments/2026/11/garden.jpg",
    });
    const pdf = item({ id: "p2", kind: "file", ref: "wiki/attachments/bill.pdf", title: "Bill", url: "/api/v1/files/wiki/attachments/bill.pdf" });
    viewer({ "GET /api/v1/workspaces/solar/items": { items: [photo, pdf] } });
    render(<Panel />);
    await screen.findByText("An e-paper display");
    const row = screen.getByRole("button", { name: "An e-paper display" }).closest("li") as HTMLElement;
    expect(row.querySelector("img.item-thumb")?.getAttribute("src")).toBe(photo.url);
    expect(row.querySelector(".item-icon")).toBeNull();
    const pdfRow = screen.getByRole("button", { name: "Bill" }).closest("li") as HTMLElement;
    expect(pdfRow.querySelector("img.item-thumb")).toBeNull();
    expect(pdfRow.querySelector(".item-icon")).not.toBeNull();
  });

  it("opens a photo item in the image view of the panel", async () => {
    const photo = item({ id: "p1", kind: "file", ref: "a.jpg", title: "Garden bed", url: "/api/v1/files/wiki/a.jpg" });
    viewer({ "GET /api/v1/workspaces/solar/items": { items: [photo] } });
    function Both() {
      const state = useDocumentPanelState();
      return (
        <MemoryRouter future={FUTURE}>
          <DocumentPanelProvider panel={state}>
            <ItemPanel slug="solar" refreshKey={0} open onClose={vi.fn()} />
            <output data-testid="view">{state.view?.type === "image" ? state.view.image.url : ""}</output>
          </DocumentPanelProvider>
        </MemoryRouter>
      );
    }
    render(<Both />);
    fireEvent.click(await screen.findByRole("button", { name: "Garden bed" }));
    expect(screen.getByTestId("view").textContent).toBe("/api/v1/files/wiki/a.jpg");
  });

  it("opens a document in the document panel", async () => {
    viewer({ "GET /api/v1/workspaces/solar/items": { items } });
    render(<Panel />);
    fireEvent.click(await screen.findByRole("button", { name: "Pie" }));
    expect(screen.getByTestId("shown").textContent).toBe("/documents/alex/pie");
  });

  it("pins from the row menu, removes with an undo window, and adds a link behind a button", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces/solar/items": { items: [item()] },
      "PATCH /api/v1/workspaces/solar/items/i1": item({ pinned: true }),
      "DELETE /api/v1/workspaces/solar/items/i1": {},
      "POST /api/v1/workspaces/solar/items": item({ id: "i9" }),
    });
    render(<Panel />);
    await screen.findByText("Quote");
    fireEvent.click(screen.getByRole("button", { name: "Actions for Quote" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin Quote" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ pinned: true });

    // Remove hides the row at once and can be undone; no call goes out.
    fireEvent.click(screen.getByRole("button", { name: "Actions for Quote" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove Quote" }));
    expect(screen.queryByRole("button", { name: "Quote" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: "Quote" })).toBeTruthy();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);

    // Left alone, the removal goes out when the time is up.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      fireEvent.click(screen.getByRole("button", { name: "Actions for Quote" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "Remove Quote" }));
      await act(async () => {
        vi.advanceTimersByTime(UNDO_MS + 50);
      });
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));

    expect(screen.queryByLabelText("Add a link")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "+ Add link" }));
    fireEvent.change(screen.getByLabelText("Add a link"), { target: { value: "https://a.test" } });
    fireEvent.change(screen.getByLabelText("Link title"), { target: { value: "A" } });
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      kind: "link",
      ref: "https://a.test",
      title: "A",
      url: "https://a.test",
    });
    expect(screen.queryByLabelText("Add a link")).toBeNull();
  });

  it("shows one line per item, with the whole title on hover, a pin mark, pinned first", async () => {
    viewer({
      "GET /api/v1/workspaces/solar/items": {
        items: [
          item({ id: "a", kind: "link", title: "A long title that gets cut", url: "https://a.test" }),
          item({ id: "b", kind: "link", title: "Pinned one", url: "https://b.test", pinned: true }),
        ],
      },
    });
    render(<Panel />);
    const long = await screen.findByRole("button", { name: "A long title that gets cut" });
    expect(long.getAttribute("title")).toBe("A long title that gets cut");
    expect(document.querySelectorAll(".item-pin").length).toBe(1);
    const rows = [...document.querySelectorAll(".item .item-open")].map((el) => el.textContent);
    expect(rows).toEqual(["Pinned one", "A long title that gets cut"]);
    // Pin and Remove are not on the row until its menu opens.
    expect(screen.queryByRole("menuitem")).toBeNull();
  });

  it("folds a group with its header, keeps that, and counts the items", async () => {
    window.localStorage.clear();
    viewer({ "GET /api/v1/workspaces/solar/items": { items } });
    const { unmount } = render(<Panel />);
    await screen.findByText("Quote");
    const toggle = screen.getByRole("button", { name: /Links/ });
    expect(toggle.textContent).toContain("2");
    fireEvent.click(toggle);
    expect(screen.queryByText("Quote")).toBeNull();
    expect(JSON.parse(window.localStorage.getItem(GROUPS_KEY) ?? "[]")).toEqual(["link"]);
    unmount();
    render(<Panel />);
    await screen.findByText("Pie");
    expect(screen.queryByText("Quote")).toBeNull();
    window.localStorage.clear();
  });

  it("marks the item open in the panel", async () => {
    viewer({ "GET /api/v1/workspaces/solar/items": { items } });
    render(<Panel />);
    const pie = await screen.findByRole("button", { name: "Pie" });
    expect(pie.getAttribute("aria-current")).toBeNull();
    fireEvent.click(pie);
    expect(screen.getByRole("button", { name: "Pie" }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByRole("button", { name: "Menu notes" }).getAttribute("aria-current")).toBeNull();
  });

  it("reloads on a finished turn and on focus", async () => {
    const { calls } = viewer({ "GET /api/v1/workspaces/solar/items": { items: [] } });
    const { rerender } = render(<Panel />);
    await screen.findByText("Nothing here yet.");
    const before = calls.length;
    rerender(<Panel refreshKey={1} />);
    await waitFor(() => expect(calls.length).toBeGreaterThan(before));
    const afterTurn = calls.length;
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(calls.length).toBeGreaterThan(afterTurn));
  });
});

describe("the workspace header", () => {
  it("shows the title and the goal, and saves an edit", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "PATCH /api/v1/workspaces/alex%2Fsolar": ws({ title: "Solar 2" }),
    });
    const onChange = vi.fn();
    render(<WorkspaceHeader workspace={ws()} onChange={onChange} onItems={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Solar install" })).toBeTruthy();
    expect(screen.getByText("Pick an installer")).toBeTruthy();
    // A personal workspace carries no mark.
    expect(document.querySelector(".shared-mark")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByText("Tasks and projects");
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Solar 2" } });
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "stay under budget" } });
    fireEvent.change(screen.getByLabelText("Messages to your phone"), {
      target: { value: "questions" },
    });
    fireEvent.click(screen.getByLabelText(/github/));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
      title: "Solar 2",
      goal: "Pick an installer",
      notes: "stay under budget",
      model: "",
      notify: "questions",
      servers: ["tasks", "github"],
    });
  });

  it("pauses, resumes and archives with a status change", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "PATCH /api/v1/workspaces/alex%2Fsolar": ws({ status: "paused" }),
    });
    render(<WorkspaceHeader workspace={ws()} onChange={vi.fn()} onItems={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ status: "paused" });
  });

  it("marks a shared workspace with the two-person icon beside the title", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    const shared = ws({ slug: "thanksgiving", title: "Thanksgiving", owner: "jake", shared: true, members: ["mia"] });
    render(
      <SessionContext.Provider value={session("jake", "Jake")}>
        <WorkspaceHeader workspace={shared} onChange={vi.fn()} onItems={vi.fn()} />
      </SessionContext.Provider>,
    );
    const heading = screen.getByRole("heading");
    expect(heading.querySelector(".shared-mark svg.j-icon")).not.toBeNull();
    // The icon stands for no text: the title is the name alone.
    expect(heading.textContent).toBe("Thanksgiving");
  });

  it("names the people of a shared workspace under the title, and not in the drawer", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    const shared = ws({ slug: "thanksgiving", title: "Thanksgiving", owner: "jake", shared: true, members: ["mia"] });
    render(
      <SessionContext.Provider value={session("jake", "Jake")}>
        <WorkspaceHeader workspace={shared} onChange={vi.fn()} onItems={vi.fn()} />
      </SessionContext.Provider>,
    );
    expect((await screen.findByText("Jake, Mia")).className).toContain("subtitle");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByText("Tasks and projects");
    // The people never change, so the drawer has no field for them.
    expect(within(document.querySelector(".ws-dialog") as HTMLElement).queryByText("People")).toBeNull();
  });

  it("changes a shared workspace by its two-segment ref", async () => {
    const shared = ws({ slug: "thanksgiving", title: "Thanksgiving", owner: "jake", shared: true, members: ["mia"] });
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "PATCH /api/v1/workspaces/shared%2Fthanksgiving": shared,
    });
    render(<WorkspaceHeader workspace={shared} onChange={vi.fn()} onItems={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")?.path).toBe("/api/v1/workspaces/shared%2Fthanksgiving");
  });

  it("sets the notify mode of the signed-in person in a shared workspace", async () => {
    const shared = ws({ slug: "thanksgiving", title: "Thanksgiving", owner: "jake", shared: true, members: ["mia"] });
    const { calls } = viewer({
      "GET /api/v1/workspaces/options": OPTIONS,
      "GET /api/v1/workspaces/shared%2Fthanksgiving/notify": { mode: "decide", default: "decide" },
      "POST /api/v1/workspaces/shared%2Fthanksgiving/notify": { mode: "all", default: "decide" },
    });
    render(<WorkspaceHeader workspace={shared} onChange={vi.fn()} onItems={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const field = (await screen.findByLabelText("When Joshua may reach you here")) as HTMLSelectElement;
    expect(field.value).toBe("decide");
    fireEvent.change(field, { target: { value: "all" } });
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST");
    expect(post?.path).toBe("/api/v1/workspaces/shared%2Fthanksgiving/notify");
    expect(post?.body).toEqual({ mode: "all" });
  });

  it("has no notify mode in a personal workspace, where nobody else writes", async () => {
    const { calls } = viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    render(<WorkspaceHeader workspace={ws()} onChange={vi.fn()} onItems={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByText("Tasks and projects");
    expect(screen.queryByLabelText("When Joshua may reach you here")).toBeNull();
    expect(calls.some((c) => c.path.endsWith("/notify"))).toBe(false);
  });

  it("points at the Profile page for the device, and holds no control of its own", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    render(<WorkspaceHeader workspace={ws()} onChange={vi.fn()} onItems={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByText("Tasks and projects");
    // The subscription is one person's and one device's, so the drawer says
    // where it lives and offers nothing of its own.
    expect(screen.getByRole("link", { name: "Profile page" }).getAttribute("href")).toEqual("/me");
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull();
  });

  it("offers Resume for a paused workspace", async () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    render(<WorkspaceHeader workspace={ws({ status: "paused" })} onChange={vi.fn()} onItems={vi.fn()} />);
    expect(screen.getByText("paused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("button", { name: "Resume" })).toBeTruthy();
  });
});

describe("the routes", () => {
  it("reserves w, so /chat/w is never the chat of a person", () => {
    expect(validPerson("w")).toBe(false);
    expect(validPerson("wendy")).toBe(true);
  });

  it("opens a workspace at /chat/w/:space/:slug with its chat on the workspace", async () => {
    const { calls } = viewer({
      "GET /api/v1/session": { person: { id: "alex", name: "Alex", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/alex%2Fsolar": ws(),
      "GET /api/v1/workspaces": { workspaces: [ws()] },
      "GET /api/v1/workspaces/alex%2Fsolar/items": { items: [item()] },
      "GET /api/v1/chat/alex/history": { messages: [], nextBefore: null },
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/alex/solar"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
          <Route path="/chat/:person" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Solar install" })).toBeTruthy();
    await screen.findByText("Quote");
    const history = calls.find((c) => c.path === "/api/v1/chat/alex/history");
    expect(history).toBeTruthy();
    // The history call names the workspace, by its two-segment ref.
    const asked = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
      .map(([u]) => String(u))
      .find((u) => u.includes("/history"));
    expect(asked).toContain("workspace=alex%2Fsolar");
  });

  it("reads a shared thread as a group chat: own messages right, the others left and named", async () => {
    const row = (id: number, role: "person" | "joshua", text: string, who?: string) => ({
      id,
      role,
      text,
      status: "ok",
      at: "2026-01-01T10:00:00Z",
      ...(who ? { who } : {}),
    });
    viewer({
      "GET /api/v1/session": { person: { id: "mia", name: "Mia", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/shared%2Fthanksgiving": ws({
        slug: "thanksgiving",
        title: "Thanksgiving",
        owner: "jake",
        shared: true,
        members: ["mia"],
      }),
      "GET /api/v1/workspaces": { workspaces: [] },
      "GET /api/v1/workspaces/shared%2Fthanksgiving/items": { items: [] },
      "GET /api/v1/chat/mia/history": {
        messages: [
          row(1, "person", "I'll bring pie", "Mia"),
          row(2, "person", "the oven is mine", "Jake"),
          row(3, "joshua", "Noted."),
        ],
        nextBefore: null,
      },
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/shared/thanksgiving"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("I'll bring pie");
    const mine = screen.getByText("I'll bring pie").closest("article");
    const other = screen.getByText("the oven is mine").closest("article");
    expect(mine?.className).toBe("msg msg-person");
    expect(other?.className).toBe("msg msg-person msg-other");
    expect(within(other as HTMLElement).getByText("Jake").className).toBe("msg-who");
    expect(mine?.querySelector(".msg-who")).toBeNull();
    // Each writer has a colour of their own.
    const tones = [mine, other].map((el) => el?.querySelector(".bubble")?.className);
    expect(tones[0]).not.toBe(tones[1]);
    // The answer is marked as Joshua's.
    const names = [...document.querySelectorAll(".msg-who")].map((el) => el.textContent);
    expect(names).toEqual(["Jake", "Joshua"]);
  });

  it("keeps the layout of a personal workspace: no name and no colour", async () => {
    viewer({
      "GET /api/v1/session": { person: { id: "alex", name: "Alex", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/alex%2Fsolar": ws(),
      "GET /api/v1/workspaces": { workspaces: [ws()] },
      "GET /api/v1/workspaces/alex%2Fsolar/items": { items: [] },
      "GET /api/v1/chat/alex/history": {
        messages: [
          { id: 1, role: "person", text: "How long?", status: "ok", at: "2026-01-01T10:00:00Z" },
          { id: 2, role: "joshua", text: "Eleven years.", status: "ok", at: "2026-01-01T10:00:01Z" },
        ],
        nextBefore: null,
      },
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/alex/solar"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("How long?");
    expect(screen.getByText("How long?").closest("article")?.className).toBe("msg msg-person");
    expect(screen.getByText("How long?").className).toBe("bubble");
    expect(document.querySelector(".msg-who")).toBeNull();
    expect(document.querySelector(".msg-other")).toBeNull();
  });

  it("marks the thread read up to the newest row on screen, and asks for the counts again", async () => {
    const thread = ws({
      slug: "thanksgiving",
      title: "Thanksgiving",
      owner: "alex",
      shared: true,
      members: ["mia"],
    });
    const { calls } = viewer({
      "GET /api/v1/session": { person: { id: "mia", name: "Mia", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/shared%2Fthanksgiving": thread,
      "GET /api/v1/workspaces": { workspaces: [{ ...thread, unread: 2 }] },
      "GET /api/v1/workspaces/shared%2Fthanksgiving/items": { items: [] },
      "GET /api/v1/chat/mia/history": {
        messages: [
          { id: 7, role: "person", text: "the oven is mine", status: "ok", at: "2026-01-01T10:00:00Z", who: "Alex" },
          { id: 8, role: "joshua", text: "Noted.", status: "ok", at: "2026-01-01T10:00:01Z" },
        ],
        nextBefore: null,
      },
      "POST /api/v1/workspaces/shared%2Fthanksgiving/read": { last_read_turn_id: 8, unread: 0 },
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/shared/thanksgiving"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("Noted.");
    // The newest row of the thread, and that row alone, is the mark.
    await waitFor(() => {
      const read = calls.filter((c) => c.path.endsWith("/read"));
      expect(read.map((c) => c.body)).toEqual([{ turn_id: 8 }]);
    });
    // The rail is loaded again, so the count beside the name is the new one.
    await waitFor(() =>
      expect(calls.filter((c) => c.path === "/api/v1/workspaces").length).toBeGreaterThan(1),
    );
  });

  it("marks nothing in the normal chat: a count belongs to a workspace thread", async () => {
    const { calls } = viewer({
      "GET /api/v1/workspaces": { workspaces: [ws()] },
      "GET /api/v1/chat/alex/history": {
        messages: [{ id: 4, role: "joshua", text: "Eleven years.", status: "ok", at: "2026-01-01T10:00:00Z" }],
        nextBefore: null,
      },
    });
    render(
      <MemoryRouter initialEntries={["/chat/alex"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/:person" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("Eleven years.");
    expect(calls.some((c) => c.path.endsWith("/read"))).toBe(false);
  });

  it("sends a one-segment link to the two-segment address of its workspace", async () => {
    viewer({
      "GET /api/v1/session": { person: { id: "mia", name: "Mia", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/thanksgiving": ws({ slug: "thanksgiving", owner: "alex", shared: true, members: ["mia"] }),
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/thanksgiving"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<Where />} />
          <Route path="/chat/w/:ref" element={<WorkspaceLink />} />
        </Routes>
      </MemoryRouter>,
    );
    expect((await screen.findByTestId("where")).textContent).toBe("/chat/w/shared/thanksgiving");
  });

  it("holds a message written while a turn of the workspace runs", async () => {
    const { calls } = viewer({
      "GET /api/v1/session": { person: { id: "alex", name: "Alex", role: "member" }, sections: [] },
      "GET /api/v1/workspaces/alex%2Fsolar": ws(),
      "GET /api/v1/workspaces": { workspaces: [ws()] },
      "GET /api/v1/workspaces/alex%2Fsolar/items": { items: [item()] },
      "GET /api/v1/chat/alex/history": {
        messages: [],
        nextBefore: null,
        pending: { turnId: "t-1", state: "running", since: new Date().toISOString(), text: "earlier" },
      },
      "GET /api/v1/chat/alex/held": { held: [] },
      "POST /api/v1/chat/alex/held": { held: [{ id: "h1", text: "one more", at: "2026-01-01T10:00:00Z" }] },
    });
    render(
      <MemoryRouter initialEntries={["/chat/w/alex/solar"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Solar install" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "one more" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Waiting to go with the next turn")).toBeTruthy();
    const add = calls.find((c) => c.method === "POST" && c.path === "/api/v1/chat/alex/held");
    expect(add?.body).toEqual({ text: "one more", workspace: "alex/solar" });
    // The hold of the thread is asked for with the workspace, and no turn went out.
    const asked = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
      .map(([u]) => String(u))
      .find((u) => u.includes("/held"));
    expect(asked).toBe("/api/v1/chat/alex/held?workspace=alex%2Fsolar");
    expect(calls.some((c) => c.path.endsWith("/turns"))).toBe(false);
  });

  it("says so for a workspace that does not exist", async () => {
    viewer({ "GET /api/v1/session": { person: { id: "alex", name: "Alex", role: "member" }, sections: [] } });
    render(
      <MemoryRouter initialEntries={["/chat/w/alex/nope"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("This workspace does not exist.")).toBeTruthy();
  });

  it("creates from the rail and goes to the new workspace", async () => {
    viewer({
      "GET /api/v1/workspaces": { workspaces: [] },
      "GET /api/v1/workspaces/options": OPTIONS,
      "POST /api/v1/workspaces": ws({ slug: "trip", title: "Trip" }),
      "GET /api/v1/chat/alex/history": { messages: [], nextBefore: null },
    });
    render(
      <MemoryRouter initialEntries={["/chat/alex"]} future={FUTURE}>
        <Routes>
          <Route path="/chat/:person" element={<ChatPage />} />
          <Route path="/chat/w/:space/:slug" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "+ New workspace" }));
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Trip" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect((await screen.findByTestId("where")).textContent).toBe("/chat/w/alex/trip");
  });
});

describe("the dialogs on a phone", () => {
  it("has a growing goal field and a footer with the buttons outside the scrolling body", () => {
    viewer({ "GET /api/v1/workspaces/options": OPTIONS });
    render(<NewWorkspace onClose={vi.fn()} onCreated={vi.fn()} />);
    const goal = screen.getByLabelText("Goal (optional)");
    expect(goal.tagName).toBe("TEXTAREA");
    expect(goal.className).toContain("grow");
    const body = document.querySelector(".ws-body") as HTMLElement;
    const footer = document.querySelector(".ws-actions") as HTMLElement;
    expect(body.contains(goal)).toBe(true);
    expect(body.contains(footer)).toBe(false);
    expect(footer.contains(screen.getByRole("button", { name: "Create" }))).toBe(true);
  });
});

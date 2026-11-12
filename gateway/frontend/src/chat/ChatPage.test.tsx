import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "./ChatPage";
import { FOCUS_REFRESH_MS } from "./useHistory";

// The v7 behavior the app turns on in main.tsx, so the router does not warn.
const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

type Row = { id: number; role: string; text: string; status?: string; at?: string; turnId?: string };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function rows(list: Row[]) {
  return list.map((r) => ({ status: "ok", at: "2026-01-01T10:00:00Z", ...r }));
}

// The document a card names, as the viewer's JSON route answers for it.
const DOC = { title: "Pumpkin soup", kind: "artifact", status: "draft", updated: "2026-02-01T10:00:00Z" };

// A fake backend. `history` answers each history call; `turn` holds the
// turn stream open until release(), then ends with `answer` as done.text.
// A `.json` path is the viewer's own route for a document, which a card
// reads its title from and the open panel polls.
function backend(history: (url: URL) => Response | Promise<Response>, answer = "the answer") {
  const enc = new TextEncoder();
  let release: () => void = () => undefined;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://test");
    if (url.pathname.endsWith("/history")) return history(url);
    if (url.pathname.endsWith(".json")) return json(DOC);
    if (init?.method === "POST") {
      const body = new ReadableStream<Uint8Array>({
        async start(controller) {
          controller.enqueue(enc.encode(`event: delta\ndata: ${JSON.stringify({ text: "work" })}\n\n`));
          await new Promise<void>((r) => (release = r));
          const done = { turn_id: "t-9", text: answer, tools: [] };
          controller.enqueue(enc.encode(`event: done\ndata: ${JSON.stringify(done)}\n\n`));
          controller.close();
        },
      });
      return new Response(body, { status: 200 });
    }
    return json({ error: "not found" }, 404);
  });
  vi.stubGlobal("fetch", fetch);
  const historyCalls = () =>
    fetch.mock.calls
      .map(([input]) => new URL(String(input), "http://test"))
      .filter((u) => u.pathname.endsWith("/history"));
  return { fetch, historyCalls, release: () => release() };
}

function open(person = "alex") {
  render(
    <MemoryRouter initialEntries={[`/chat/${person}`]} future={ROUTER_FUTURE}>
      <Routes>
        <Route path="/chat/:person" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function frame() {
  return document.querySelector(".doc-panel-frame") as HTMLIFrameElement | null;
}

let now = 1_000_000;

describe("ChatPage history", () => {
  beforeEach(() => {
    now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads the newest page on mount and renders each role", async () => {
    const { historyCalls } = backend(() =>
      json({
        messages: rows([
          { id: 1, role: "person", text: "hello" },
          { id: 2, role: "joshua", text: "**hi** there" },
          { id: 3, role: "system", text: "Morning summary" },
          { id: 4, role: "joshua", text: "[error] no answer", status: "failed" },
        ]),
        nextBefore: null,
      }),
    );
    open();
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(await screen.findByText("hello")).toBeTruthy();
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    const system = screen.getByText("Morning summary");
    expect(system.className).toBe("system-line");
    expect(screen.getByText("[error] no answer").className).toContain("msg-error");
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load earlier" })).toBeNull();
    expect(historyCalls()[0]?.search).toBe("?limit=50");
    expect(localStorage.getItem("joshua-chat:last-person")).toBe("alex");
    expect(Object.keys(localStorage).filter((k) => k.includes("history"))).toEqual([]);
  });

  it("shows a failed load inline with Retry, and sending still works", async () => {
    let fail = true;
    const { historyCalls, fetch, release } = backend(() =>
      fail
        ? json({ error: "Joshua is unreachable" }, 503)
        : json({ messages: rows([{ id: 1, role: "person", text: "back" }]), nextBefore: null }),
    );
    open();
    expect(await screen.findByText(/could not be loaded: Joshua is unreachable/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "still here" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    await act(async () => release());
    fail = false;
    fireEvent.click(screen.getAllByRole("button", { name: "Retry" })[0] as HTMLElement);
    expect(await screen.findByText("back")).toBeTruthy();
    expect(historyCalls().length).toBeGreaterThanOrEqual(2);
  });

  it("refreshes on focus at most once per 10 s", async () => {
    const { historyCalls } = backend(() => json({ messages: [], nextBefore: null }));
    open();
    await waitFor(() => expect(historyCalls()).toHaveLength(1));
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(historyCalls()).toHaveLength(1);
    now += FOCUS_REFRESH_MS;
    act(() => {
      window.dispatchEvent(new Event("focus"));
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(historyCalls()).toHaveLength(2));
    now += FOCUS_REFRESH_MS;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(historyCalls()).toHaveLength(3));
  });

  it("does not refresh while a turn runs, and replaces the turn with the server rows after done", async () => {
    let serverRows: Row[] = [{ id: 1, role: "person", text: "earlier" }];
    const { historyCalls, release } = backend(() => json({ messages: rows(serverRows), nextBefore: null }));
    open();
    expect(await screen.findByText("earlier")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "new question" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("work")).toBeTruthy();
    now += FOCUS_REFRESH_MS * 3;
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(historyCalls()).toHaveLength(1);
    serverRows = [
      { id: 1, role: "person", text: "earlier" },
      { id: 2, role: "person", text: "new question", turnId: "t-9" },
      { id: 3, role: "joshua", text: "the server answer", turnId: "t-9" },
    ];
    await act(async () => release());
    expect(await screen.findByText("the server answer")).toBeTruthy();
    expect(historyCalls()).toHaveLength(2);
    expect(screen.getAllByText("new question")).toHaveLength(1);
    expect(screen.queryByText("the answer")).toBeNull();
  });

  it("loads the page before with nextBefore and prepends it", async () => {
    const { historyCalls } = backend((url) =>
      url.searchParams.get("before") === "5"
        ? json({ messages: rows([{ id: 3, role: "person", text: "oldest" }]), nextBefore: null })
        : json({ messages: rows([{ id: 5, role: "person", text: "newest" }]), nextBefore: 5 }),
    );
    open();
    expect(await screen.findByText("newest")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load earlier" }));
    expect(await screen.findByText("oldest")).toBeTruthy();
    expect(historyCalls()[1]?.search).toBe("?limit=50&before=5");
    const texts = screen.getAllByText(/oldest|newest/).map((e) => e.textContent);
    expect(texts).toEqual(["oldest", "newest"]);
    expect(screen.queryByRole("button", { name: "Load earlier" })).toBeNull();
  });

  it("shows a failed Load earlier in a toast and keeps the list", async () => {
    backend((url) =>
      url.searchParams.get("before")
        ? json({ error: "Joshua is unreachable" }, 503)
        : json({ messages: rows([{ id: 5, role: "person", text: "newest" }]), nextBefore: 5 }),
    );
    open();
    expect(await screen.findByText("newest")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load earlier" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Joshua is unreachable");
    expect(screen.getByText("newest")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Load earlier" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("ChatPage shell", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sits in the viewer's navigation, with Chat current and every section a link", async () => {
    const session = {
      person: { id: "alex", name: "Alex", role: "member" },
      anonymous: false,
      sections: [
        { key: "wiki", label: "Wiki", path: "/wiki/" },
        { key: "documents", label: "Documents", path: "/documents" },
        { key: "chat", label: "Chat", path: "/chat/alex" },
      ],
      chat_url: "/chat/alex",
      timezone: "UTC",
    };
    const { fetch } = backend(() => json({ messages: rows([{ id: 1, role: "person", text: "hello" }]), nextBefore: null }));
    const plain = fetch.getMockImplementation();
    fetch.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://test");
      if (url.pathname === "/api/v1/session") return json(session);
      return plain ? plain(input, init) : json({}, 404);
    });
    open();
    expect(await screen.findByText("hello")).toBeTruthy();
    const nav = (await screen.findAllByRole("navigation", { name: "Sections" }))[0] as HTMLElement;
    expect(nav.querySelector('[aria-current="page"]')?.textContent).toBe("Chat");
    expect(screen.getAllByRole("link", { name: "Wiki" })[0]?.getAttribute("href")).toBe("/wiki/");
    expect(screen.getAllByRole("link", { name: "Documents" })[0]?.getAttribute("href")).toBe("/documents");
    expect(screen.getAllByRole("link", { name: "Chat" })[0]?.getAttribute("href")).toBe("/chat/alex");
  });
});

describe("ChatPage document panel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the document of a card in a panel beside the conversation", async () => {
    const answer = "the recipe is ready\n\n```artifact\ndocuments/sam/pumpkin-soup.md\n```\n";
    backend(() => json({ messages: rows([{ id: 1, role: "joshua", text: answer }]), nextBefore: null }));
    open();
    const path = await screen.findByText("documents/sam/pumpkin-soup.md");
    const card = path.closest(".artifact-card") as HTMLElement;
    // The card carries the document's own title, from the viewer.
    expect(await screen.findByText("Pumpkin soup")).toBeTruthy();
    expect(card.querySelector(".artifact-path")?.textContent).toBe("documents/sam/pumpkin-soup.md");
    fireEvent.click(card.querySelector(".artifact-open") as HTMLButtonElement);
    // The viewer's own page, loaded by the frame.
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    // The conversation is still there, under the panel.
    expect(screen.getByText("the recipe is ready")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    expect(document.querySelector(".doc-panel")).toBeNull();
    expect(screen.getByText("the recipe is ready")).toBeTruthy();
  });

  // The refine loop inside one turn: comment on lines in the panel, ask for
  // the comments to be applied, and the panel shows the new body at the end
  // of the same turn.
  it("reloads the open frame once when the answer writes that same document", async () => {
    const answer = "applied\n\n```artifact\ndocuments/sam/pumpkin-soup.md\n```\n";
    const { fetch, release } = backend(
      () => json({ messages: rows([{ id: 1, role: "joshua", text: answer }]), nextBefore: null }),
      answer,
    );
    open();
    const path = await screen.findByText("documents/sam/pumpkin-soup.md");
    fireEvent.click((path.closest(".artifact-card") as HTMLElement).querySelector(".artifact-open") as HTMLButtonElement);
    const before = frame();
    expect(before?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    const list = screen.getByRole("log");
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "apply my comments" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => release());
    // A fresh query, on a new frame: no navigation the person can go back
    // through, and nothing cached.
    await waitFor(() => expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1"));
    expect(frame()).not.toBe(before);
    // The conversation was not remounted, so it keeps its scroll position.
    expect(screen.getByRole("log")).toBe(list);
    // The panel watches the document with the viewer's JSON route, and
    // takes the stamp of what the turn wrote as its baseline, so it does not
    // load this document a second time.
    const asked = fetch.mock.calls.filter(([u]) => String(u) === "/documents/sam/pumpkin-soup.json");
    expect(asked.length).toBeGreaterThan(0);
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
  });

  it("does not reload when the answer writes another document", async () => {
    const card = "here it is\n\n```artifact\ndocuments/sam/pumpkin-soup.md\n```\n";
    const answer = "a new one\n\n```artifact\ndocuments/sam/notes.md\n```\n";
    const { historyCalls, release } = backend(
      () => json({ messages: rows([{ id: 1, role: "joshua", text: card }]), nextBefore: null }),
      answer,
    );
    open();
    const path = await screen.findByText("documents/sam/pumpkin-soup.md");
    fireEvent.click((path.closest(".artifact-card") as HTMLElement).querySelector(".artifact-open") as HTMLButtonElement);
    const before = frame();
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "write my notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => release());
    // The turn has finished: it refreshed the history.
    await waitFor(() => expect(historyCalls()).toHaveLength(2));
    // The other document only changes the card; the panel keeps the
    // document it has open, on the frame it had.
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    expect(frame()).toBe(before);
  });
});

describe("ChatPage workspace", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("passes the workspace to the history call and to the turn body", async () => {
    const { fetch, historyCalls, release } = backend(() => json({ messages: [], nextBefore: null }));
    render(
      <MemoryRouter initialEntries={["/chat/alex"]} future={ROUTER_FUTURE}>
        <Routes>
          <Route path="/chat/:person" element={<ChatPage workspace="notes" />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(historyCalls()).toHaveLength(1));
    expect(historyCalls()[0]?.search).toBe("?limit=50&workspace=notes");
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => release());
    const turn = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(turn?.[0])).toBe("/api/v1/chat/alex/turns");
    expect(JSON.parse(String(turn?.[1]?.body))).toEqual({ text: "hi", workspace: "notes" });
  });

  it("sends no workspace by default", async () => {
    const { fetch, historyCalls, release } = backend(() => json({ messages: [], nextBefore: null }));
    open();
    await waitFor(() => expect(historyCalls()).toHaveLength(1));
    expect(historyCalls()[0]?.search).toBe("?limit=50");
    fireEvent.change(screen.getByLabelText("Message to Joshua"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => release());
    const turn = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(turn?.[1]?.body))).toEqual({ text: "hi" });
  });
});

describe("ChatPage person id", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("links back to the picker for an id that is not valid", () => {
    backend(() => json({ messages: [], nextBefore: null }));
    open("Not-Valid");
    expect(screen.getByText(/not a valid person id/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Choose another" }).getAttribute("href")).toBe("/chat");
  });
});

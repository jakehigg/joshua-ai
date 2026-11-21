import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Workspace } from "../api/workspaces";
import ChatList, { ALL_KEY } from "./ChatList";

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function ws(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w1",
    slug: "solar",
    title: "Solar install",
    owner: "alex",
    goal: "",
    notes: "",
    servers: [],
    model: null,
    notify: "none",
    style: "none",
    status: "active",
    shared: false,
    members: [],
    ...over,
  };
}

// A workspace that moved at hour `hour` of one day.
const at = (id: string, hour: number, over: Partial<Workspace> = {}) =>
  ws({ id, slug: id, title: id, updated_at: `2026-01-01T${String(hour).padStart(2, "0")}:00:00Z`, ...over });

function list(rows: Workspace[], at = "/chat/alex") {
  vi.stubGlobal("fetch", vi.fn(async () => json({ workspaces: rows })));
  render(
    <MemoryRouter initialEntries={[at]} future={FUTURE}>
      <ChatList chatPath="/chat/alex" />
    </MemoryRouter>,
  );
}

const names = () =>
  [...document.querySelectorAll(".v-chats .j-nav-item")].map((el) =>
    el.querySelector(".v-chat-name")?.textContent ?? el.textContent,
  );

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the list of the threads", () => {
  it("holds the normal chat, five workspaces, Show all and New workspace", async () => {
    list([at("a", 1), at("b", 2), at("c", 3), at("d", 4), at("e", 5), at("f", 6), at("g", 7)]);
    await waitFor(() => expect(screen.getByRole("link", { name: "g" })).toBeTruthy());
    // The newest activity first, five of them, with the normal chat above and
    // the two rows below.
    expect(names()).toEqual(["Chat", "g", "f", "e", "d", "c", "Show all", "+ New workspace"]);
    expect(screen.getByRole("link", { name: "Chat" }).getAttribute("href")).toBe("/chat/alex");
    // The link of a workspace is its permanent address, its id.
    expect(screen.getByRole("link", { name: "g" }).getAttribute("href")).toBe("/chat/w/g");
    expect(screen.getByRole("link", { name: "+ New workspace" }).getAttribute("href")).toBe(
      "/chat/alex?new=1",
    );
  });

  it("shows every active workspace on Show all, and folds back on Show less", async () => {
    list([at("a", 1), at("b", 2), at("c", 3), at("d", 4), at("e", 5), at("f", 6)]);
    await waitFor(() => expect(screen.getByRole("link", { name: "f" })).toBeTruthy());
    expect(screen.queryByRole("link", { name: "a" })).toBeNull();
    const more = screen.getByRole("button", { name: "Show all" });
    fireEvent.click(more);
    expect(names()).toEqual(["Chat", "f", "e", "d", "c", "b", "a", "Show less", "+ New workspace"]);
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(screen.queryByRole("link", { name: "a" })).toBeNull();
  });

  it("keeps a paused workspace in the list and holds an archived one for Show all", async () => {
    list([at("a", 1), at("b", 2, { status: "paused" }), at("c", 3, { status: "archived" })]);
    await waitFor(() => expect(screen.getByRole("link", { name: "a" })).toBeTruthy());
    // A paused workspace is a thread a person still reads and writes in.
    expect(names()).toEqual(["Chat", "b", "a", "Show all", "+ New workspace"]);
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(names()).toEqual(["Chat", "b", "a", "c", "Show less", "+ New workspace"]);
    // The archived one says so.
    expect(
      screen.getByRole("link", { name: /^c/ }).querySelector(".v-chat-status")?.textContent,
    ).toBe("archived");
  });

  it("shows the workspace on screen, however old it is", async () => {
    list(
      [at("a", 1), at("b", 2), at("c", 3), at("d", 4), at("e", 5), at("f", 6)],
      "/chat/w/a",
    );
    await waitFor(() => expect(screen.getByRole("link", { name: "f" })).toBeTruthy());
    // The oldest of the six, and the thread on screen: it stands at the end of
    // the five.
    expect(names()).toEqual(["Chat", "f", "e", "d", "c", "b", "a", "Show all", "+ New workspace"]);
    expect(screen.getByRole("link", { name: "a" }).getAttribute("aria-current")).toBe("page");
  });

  it("shows an archived workspace on screen, and a paused one, by its slug address", async () => {
    list([at("a", 1, { status: "archived", owner: "alex" })], "/chat/w/alex/a");
    await waitFor(() => expect(screen.getByRole("link", { name: /^a/ })).toBeTruthy());
    expect(screen.getByRole("link", { name: /^a/ }).getAttribute("aria-current")).toBe("page");
  });

  it("keeps Show all open after a reload", async () => {
    list([at("a", 1), at("b", 2), at("c", 3), at("d", 4), at("e", 5), at("f", 6)]);
    await waitFor(() => expect(screen.getByRole("link", { name: "f" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(window.localStorage.getItem(ALL_KEY)).toBe("1");
    cleanup();
    list([at("a", 1), at("b", 2), at("c", 3), at("d", 4), at("e", 5), at("f", 6)]);
    await waitFor(() => expect(screen.getByRole("link", { name: "a" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "Show less" })).toBeTruthy();
  });

  it("marks the thread on screen, and only that one", async () => {
    list([ws({ id: "w1", title: "Solar install" })]);
    await waitFor(() => expect(screen.getByRole("link", { name: "Solar install" })).toBeTruthy());
    expect(screen.getByRole("link", { name: "Chat" }).getAttribute("aria-current")).toBe("page");
    expect(
      screen.getByRole("link", { name: "Solar install" }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("carries the shared mark, the charm and the count in one group at the right edge", async () => {
    list([
      ws({
        shared: true,
        members: ["mia"],
        unread: 3,
        charm: { state: "working", reason: "developer task running" },
      }),
      ws({ id: "w2", title: "Trip" }),
    ]);
    const link = await waitFor(() => screen.getByRole("link", { name: /Solar install/ }));
    expect(link.querySelector(".v-chat-name")?.textContent).toBe("Solar install");
    const marks = link.querySelector(".v-chat-marks") as HTMLElement;
    expect([...marks.children].map((el) => el.className.split(" ")[0])).toEqual([
      "shared-mark",
      "work-charm",
      "v-chat-unread",
    ]);
    expect(link.querySelector(".v-chat-unread")?.textContent).toBe("3");
    // A thread with nothing unread carries no count at all.
    expect(screen.getByRole("link", { name: "Trip" }).querySelector(".v-chat-unread")).toBeNull();
  });
});

/**
 * The tap on a notification: the worker sends the open app one message, and
 * the app routes there.
 *
 * jsdom has no service worker, so the container is a plain `EventTarget` here
 * and the test dispatches the message the worker would send. The routes
 * themselves are the app's: a probe beside them says where the router went.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type { WikiPageData } from "./pages/WikiPage";

const SESSION = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const PAGE: WikiPageData = {
  kind: "page",
  root: "wiki",
  rel: "wiki/recipes/thanksgiving-turkey-kevin-belton.md",
  title: "Thanksgiving turkey",
  frontmatter: { kind: "wiki" },
  markdown: "# Thanksgiving turkey\n",
  crumbs: [],
  siblings: [],
  can_edit: false,
  edit_url: "",
  delete_csrf: "",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Where the router stands, beside the routes of the app. */
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}

/**
 * The app at `/me`, with the service worker of the installed app faked. The
 * answer sends the message the worker sends after a tap.
 */
function openApp() {
  vi.stubGlobal("fetch", (path: string) => {
    if (path === "/api/v1/session") return Promise.resolve(json(SESSION));
    if (path.startsWith("/api/v1/wiki")) return Promise.resolve(json(PAGE));
    // Every other call of a page of the app: an empty answer is enough here.
    return Promise.resolve(json({}));
  });
  const container = new EventTarget();
  Object.defineProperty(navigator, "serviceWorker", { value: container, configurable: true });

  render(
    <MemoryRouter initialEntries={["/me"]}>
      <App />
      <Where />
    </MemoryRouter>,
  );

  return (data: unknown) => container.dispatchEvent(new MessageEvent("message", { data }));
}

function where(): string {
  return screen.getByTestId("where").textContent ?? "";
}

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("a tap on a notification", () => {
  it("routes to the path the worker names", async () => {
    const send = openApp();
    await vi.waitFor(() => expect(where()).toBe("/me"));

    send({ type: "joshua:navigate", url: "/wiki/recipes/x" });

    await vi.waitFor(() => expect(where()).toBe("/wiki/recipes/x"));
  });

  it("ignores an address of another site", async () => {
    const send = openApp();
    await vi.waitFor(() => expect(where()).toBe("/me"));

    send({ type: "joshua:navigate", url: "https://evil.test/wiki" });
    send({ type: "joshua:navigate", url: "//evil.test/wiki" });

    expect(where()).toBe("/me");
  });

  it("ignores a message of another kind", async () => {
    const send = openApp();
    await vi.waitFor(() => expect(where()).toBe("/me"));

    send({ type: "joshua:something-else", url: "/wiki/recipes/x" });
    send("joshua:navigate");

    expect(where()).toBe("/me");
  });
});

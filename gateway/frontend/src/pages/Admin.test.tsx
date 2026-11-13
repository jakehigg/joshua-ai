import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import type { Session } from "../shell/Shell";
import { ONLY_OWNER, SECRET_NOT_SET, SECRET_SET, type AdminUsers } from "./admin/Users";

/** The owner who manages the roster, and a member who may not. */
function session(role: string): Session {
  return {
    person: { id: "mira", name: "Mira", role },
    anonymous: false,
    sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
    chat_url: "/chat/mira",
    timezone: "America/New_York",
  };
}

/** Three invented people: an owner, a member, and a guest with no secret. */
const ROSTER: AdminUsers = {
  users: [
    { id: "mira", name: "Mira", role: "owner", has_secret: true },
    { id: "tomas", name: "Tomas", role: "member", has_secret: true },
    { id: "iris", name: "Iris", role: "guest", has_secret: false },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The session of the person, the roster, and every write of the page. The
 * list it gives back holds the address and the body of each write.
 */
function stubRoutes(role: string, roster: AdminUsers = ROSTER) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path === "/api/v1/session") {
      return Promise.resolve(json(session(role)));
    }
    if (path === "/api/v1/admin/users" && init?.method !== "POST") {
      return Promise.resolve(json(roster));
    }
    if (path.startsWith("/api/v1/admin/users")) {
      sent.push({ path, body: init?.body === undefined ? null : JSON.parse(String(init.body)) });
      return Promise.resolve(json({ ok: true }));
    }
    return Promise.resolve(json({ links: [] }));
  });
  return sent;
}

function app(address = "/admin/users") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <App />
    </MemoryRouter>,
  );
}

/** The row of one person of the table. */
function row(name: string): HTMLElement {
  return screen.getByText(name).closest("tr") as HTMLElement;
}

/** The action buttons of a row, in the order the row shows them. */
function actions(name: string): string[] {
  return [...row(name).querySelectorAll("td.v-user-actions button")].map(
    (button) => button.textContent ?? "",
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the admin Users section", () => {
  it("shows the two columns, with only Users built", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("Mira")).toBeDefined();
    const nav = screen.getByRole("navigation", { name: "Admin sections" });
    expect(within(nav).getByRole("link", { name: "Users" }).getAttribute("href")).toEqual(
      "/admin/users",
    );
    expect([...nav.querySelectorAll(".v-admin-soon")].map((item) => item.textContent)).toEqual([
      "MCP serverssoon",
      "Channelssoon",
      "Instancesoon",
      "Usagesoon",
    ]);
    // A placeholder is a line and not a link: there is nothing to follow.
    expect(within(nav).getAllByRole("link")).toHaveLength(1);
  });

  it("shows the roster, each person with their role and their secret", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("Iris")).toBeDefined();
    expect(within(row("Mira")).getByText("owner")).toBeDefined();
    expect(within(row("Tomas")).getByText("member")).toBeDefined();
    expect(within(row("Tomas")).getByText(SECRET_SET)).toBeDefined();
    expect(within(row("Iris")).getByText("guest")).toBeDefined();
  });

  it("says that the person with no secret cannot sign in, and leads with the secret", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("Iris")).toBeDefined();
    const iris = within(row("Iris"));
    expect(iris.getByText(SECRET_NOT_SET).textContent).toEqual("Not set — can't sign in");
    // The row leads with the one thing that lets Iris in, and not with the role.
    expect(actions("Iris")).toEqual(["Set secret", "Promote", "Remove"]);
    expect(iris.getByRole("button", { name: "Set secret" }).className).toEqual(
      "j-btn sm primary",
    );
    // A person who can sign in leads with their role instead.
    expect(actions("Tomas")).toEqual(["Promote", "Set secret", "Remove"]);
    expect(
      within(row("Tomas")).getByRole("button", { name: "Set secret" }).className,
    ).toEqual("j-btn sm");
    expect(actions("Mira")).toEqual(["Demote", "Set secret", "Remove"]);
  });

  it("sets the secret of the person who has none", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("Iris")).toBeDefined();
    fireEvent.click(within(row("Iris")).getByRole("button", { name: "Set secret" }));
    fireEvent.change(document.querySelector('input[name="secret"]') as HTMLInputElement, {
      target: { value: "a long one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(sent).toEqual([
        { path: "/api/v1/admin/users/iris/secret", body: { secret: "a long one" } },
      ]),
    );
  });

  it("promotes a guest one tier, and demotes an owner", async () => {
    const sent = stubRoutes("owner", {
      users: [...ROSTER.users, { id: "noor", name: "Noor", role: "owner", has_secret: true }],
    });

    app();

    expect(await screen.findByText("Iris")).toBeDefined();
    fireEvent.click(within(row("Iris")).getByRole("button", { name: "Promote" }));
    await waitFor(() =>
      expect(sent).toEqual([{ path: "/api/v1/admin/users/iris/role", body: { role: "member" } }]),
    );

    fireEvent.click(within(row("Mira")).getByRole("button", { name: "Demote" }));

    await waitFor(() =>
      expect(sent).toEqual([
        { path: "/api/v1/admin/users/iris/role", body: { role: "member" } },
        { path: "/api/v1/admin/users/mira/role", body: { role: "member" } },
      ]),
    );
  });

  it("will not demote or remove the only owner", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("Mira")).toBeDefined();
    const demote = within(row("Mira")).getByRole("button", { name: "Demote" });
    const remove = within(row("Mira")).getByRole("button", { name: "Remove" });
    expect((demote as HTMLButtonElement).disabled).toBe(true);
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    expect(demote.getAttribute("title")).toBe(ONLY_OWNER);
    fireEvent.click(demote);
    fireEvent.click(remove);
    expect(sent).toEqual([]);
    expect(screen.queryByText(/Remove Mira\?/)).toBeNull();
  });

  it("adds a person with no handle", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("Mira")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Add a person" }));
    const dialog = within(screen.getByRole("dialog", { name: "Add a person" }));
    const handle = document.querySelector('input[name="handle"]') as HTMLInputElement;
    expect(handle.required).toBe(false);
    fireEvent.change(document.querySelector('input[name="name"]') as HTMLInputElement, {
      target: { value: "Nadia" },
    });
    fireEvent.change(document.querySelector('input[name="id"]') as HTMLInputElement, {
      target: { value: "nadia" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(sent).toEqual([
        {
          path: "/api/v1/admin/users",
          body: { id: "nadia", name: "Nadia", handle: "", role: "member", secret: "" },
        },
      ]),
    );
  });

  it("removes a person once the owner says so", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("Tomas")).toBeDefined();
    fireEvent.click(within(row("Tomas")).getByRole("button", { name: "Remove" }));
    // The row asks first, and nothing is sent until the second Remove.
    expect(sent).toEqual([]);
    fireEvent.click(screen.getByText(/Remove Tomas\?/).querySelector("button") as HTMLElement);

    await waitFor(() =>
      expect(sent).toEqual([{ path: "/api/v1/admin/users/tomas/remove", body: {} }]),
    );
  });

  it("adds a person, their role and their secret in one dialog", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("Mira")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Add a person" }));
    const dialog = within(screen.getByRole("dialog", { name: "Add a person" }));
    const type = (field: string, value: string) =>
      fireEvent.change(document.querySelector(`input[name="${field}"]`) as HTMLInputElement, {
        target: { value },
      });
    type("name", "Nadia");
    type("id", "nadia");
    type("handle", "telegram:nadia");
    type("secret", "a long one");
    fireEvent.click(dialog.getByRole("button", { name: "guest" }));
    fireEvent.click(dialog.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(sent).toEqual([
        {
          path: "/api/v1/admin/users",
          body: {
            id: "nadia",
            name: "Nadia",
            handle: "telegram:nadia",
            role: "guest",
            secret: "a long one",
          },
        },
      ]),
    );
  });

  it("gives a member nothing at all at the address", async () => {
    stubRoutes("member");

    app();

    // What an address no route claims gives: no page, and no refusal either.
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(screen.queryByRole("navigation", { name: "Admin sections" })).toBeNull();
    expect(screen.queryByText("Mira")).toBeNull();
    expect(screen.queryByText(/forbidden/i)).toBeNull();
    expect(document.querySelector("main")).toBeNull();
  });
});

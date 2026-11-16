import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../App";
import type { Session } from "../../shell/Shell";
import { SECRET_NOT_SET, SECRET_SET, type AdminSetting, type AdminSettings } from "./Instance";

/** The owner who writes the settings, and a member who may not. */
function session(role: string): Session {
  return {
    person: { id: "mira", name: "Mira", role },
    anonymous: false,
    sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
    chat_url: "/chat/mira",
    timezone: "America/New_York",
  };
}

/** The four push settings: two plain ones, a secret, and the switch. */
const PUBLIC_KEY: AdminSetting = {
  key: "push.vapid_public_key",
  label: "VAPID public key",
  hint: "The browser subscribes with this key.",
  kind: "text",
  is_secret: false,
  is_set: true,
  value: "a-public-key",
  updated_at: "2026-11-15T10:00:00+00:00",
  updated_by: "mira",
};
const PRIVATE_KEY: AdminSetting = {
  key: "push.vapid_private_key",
  label: "VAPID private key",
  hint: "",
  kind: "secret",
  is_secret: true,
  is_set: false,
  value: null,
  updated_at: null,
  updated_by: null,
};
const SUBJECT: AdminSetting = {
  key: "push.vapid_subject",
  label: "VAPID subject",
  hint: "",
  kind: "text",
  is_secret: false,
  is_set: true,
  value: "mailto:mira@example.test",
  updated_at: null,
  updated_by: null,
};
const ENABLED: AdminSetting = {
  key: "push.enabled",
  label: "Push notifications on",
  hint: "Turn this on once the three keys above are set.",
  kind: "bool",
  is_secret: false,
  is_set: false,
  value: "",
  updated_at: null,
  updated_by: null,
};

/** One group of the two, as the route answers it. */
function answer(...settings: AdminSetting[]): AdminSettings {
  return { groups: [{ prefix: "push", label: "Push notifications", settings }] };
}

const SETTINGS = answer(PUBLIC_KEY, PRIVATE_KEY, SUBJECT, ENABLED);
/** The same four, with the private key stored and push on. */
const WORKING = answer(PUBLIC_KEY, { ...PRIVATE_KEY, is_set: true }, SUBJECT, {
  ...ENABLED,
  is_set: true,
  value: "true",
});
/** The four with the private key stored, so the switch is the step that is left. */
const READY = answer(PUBLIC_KEY, { ...PRIVATE_KEY, is_set: true }, SUBJECT, ENABLED);

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The session of the person, the settings, and every write of the page. The
 * list it gives back holds the body of each write; `status` refuses one.
 */
function stubRoutes(role: string, settings: AdminSettings = SETTINGS, status = 200) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path === "/api/v1/session") {
      return Promise.resolve(json(session(role)));
    }
    if (path === "/api/v1/admin/settings" && init?.method !== "POST") {
      return Promise.resolve(json(settings));
    }
    if (path === "/api/v1/admin/settings") {
      sent.push({ path, body: init?.body === undefined ? null : JSON.parse(String(init.body)) });
      if (status !== 200) {
        return Promise.resolve(json({ error: "JOSHUA_SETTINGS_KEY is not set" }, status));
      }
      return Promise.resolve(json({ setting: {} }));
    }
    return Promise.resolve(json({ links: [] }));
  });
  return sent;
}

function app(address = "/admin/instance") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <App />
    </MemoryRouter>,
  );
}

/** The field of one setting, by the name the input carries. */
function input(key: string): HTMLInputElement | null {
  return document.querySelector(`input[name="${key}"]`);
}

/** The form of one setting, by the label it shows. */
function form(label: string): HTMLElement {
  return screen.getByText(label).closest("form") as HTMLElement;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the admin Instance section", () => {
  it("shows a section for each group, with a field for each setting", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("Push notifications")).toBeDefined();
    expect(input("push.vapid_public_key")?.value).toEqual("a-public-key");
    expect(screen.getByText(/changed .* by mira/)).toBeDefined();
  });

  it("says whether a secret is set, and never shows the stored one", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("VAPID private key")).toBeDefined();
    expect(within(form("VAPID private key")).getByText(SECRET_NOT_SET)).toBeDefined();
    // No field at all until the owner asks to write one.
    expect(input("push.vapid_private_key")).toBeNull();
    // The step the owner has not taken: the primary button, and it says what
    // it sets. A grey "Set" under a hint is what nobody found.
    const button = within(form("VAPID private key")).getByRole("button", {
      name: "Set the private key",
    });
    expect(button.className).toContain("primary");
  });

  it("says what is still to do, and links the docs", async () => {
    stubRoutes("owner");

    app();

    expect(await screen.findByText("Push is off: the private key is not set.")).toBeDefined();
    expect(screen.getByRole("link", { name: "notifications.md" }).getAttribute("href")).toEqual(
      "/wiki/joshua-docs/notifications.md",
    );
  });

  it("turns push on with one tap of the checkbox", async () => {
    const sent = stubRoutes("owner", READY);

    app();

    const box = (await screen.findByLabelText("Push notifications on")) as HTMLInputElement;
    expect(box.type).toEqual("checkbox");
    expect(box.checked).toEqual(false);
    fireEvent.click(box);

    await waitFor(() =>
      expect(sent).toEqual([
        { path: "/api/v1/admin/settings", body: { key: "push.enabled", value: "true" } },
      ]),
    );
  });

  it("turns push off again, and says that it was on", async () => {
    const sent = stubRoutes("owner", WORKING);

    app();

    const line = "The three keys are set and push is on. Each person turns their own devices on.";
    expect(await screen.findByText(line)).toBeDefined();
    const box = screen.getByLabelText("Push notifications on") as HTMLInputElement;
    expect(box.checked).toEqual(true);
    fireEvent.click(box);

    await waitFor(() =>
      expect(sent).toEqual([
        { path: "/api/v1/admin/settings", body: { key: "push.enabled", value: "false" } },
      ]),
    );
  });

  it("refuses a subject that is no mailto: and no https: address", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("VAPID subject")).toBeDefined();
    const field = input("push.vapid_subject") as HTMLInputElement;
    expect(field.placeholder).toEqual("mailto:you@example.com");
    fireEvent.change(field, { target: { value: "mira@example.test" } });
    fireEvent.click(within(form("VAPID subject")).getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/must start with mailto: or https:\/\//)).toBeDefined();
    expect(sent).toEqual([]);
  });

  it("writes a plain setting", async () => {
    const sent = stubRoutes("owner");

    app();

    expect(await screen.findByText("VAPID public key")).toBeDefined();
    fireEvent.change(input("push.vapid_public_key") as HTMLInputElement, {
      target: { value: "another-public-key" },
    });
    fireEvent.click(within(form("VAPID public key")).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(sent).toEqual([
        {
          path: "/api/v1/admin/settings",
          body: { key: "push.vapid_public_key", value: "another-public-key" },
        },
      ]),
    );
  });

  it("replaces a secret with the one the owner types", async () => {
    const sent = stubRoutes("owner", answer(PUBLIC_KEY, { ...PRIVATE_KEY, is_set: true }));

    app();

    expect(await screen.findByText("VAPID private key")).toBeDefined();
    const secret = within(form("VAPID private key"));
    expect(secret.getByText(SECRET_SET)).toBeDefined();
    fireEvent.click(secret.getByRole("button", { name: "Replace" }));
    const field = input("push.vapid_private_key") as HTMLInputElement;
    expect(field.type).toEqual("password");
    expect(field.value).toEqual("");
    fireEvent.change(field, { target: { value: "a new private key" } });
    fireEvent.click(secret.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(sent).toEqual([
        {
          path: "/api/v1/admin/settings",
          body: { key: "push.vapid_private_key", value: "a new private key" },
        },
      ]),
    );
  });

  it("shows the refusal of a secret write with no key on the Joshua side", async () => {
    stubRoutes("owner", SETTINGS, 503);

    app();

    expect(await screen.findByText("VAPID private key")).toBeDefined();
    const secret = within(form("VAPID private key"));
    fireEvent.click(secret.getByRole("button", { name: "Set the private key" }));
    fireEvent.change(input("push.vapid_private_key") as HTMLInputElement, {
      target: { value: "a new private key" },
    });
    fireEvent.click(secret.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("JOSHUA_SETTINGS_KEY is not set")).toBeDefined();
  });

  it("gives a member nothing at all at the address", async () => {
    stubRoutes("member");

    app();

    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(screen.queryByText("Push notifications")).toBeNull();
    expect(document.querySelector("main")).toBeNull();
  });
});

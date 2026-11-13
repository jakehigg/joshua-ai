import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProfileSettings, { type Profile } from "./ProfileSettings";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PROFILE: Profile = { prompt: "Keep it short.", journal: "auto", in_config: false };

/** The routes the cards call. The list holds each body the page posted, by path. */
function stubRoutes({
  method = "pin",
  profile = PROFILE,
  secret = () => json({ ok: true }),
}: {
  method?: string;
  profile?: Profile;
  secret?: () => Response;
} = {}) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      sent.push({ path, body: JSON.parse(String(init.body)) });
    }
    if (path === "/api/v1/auth-method") {
      return Promise.resolve(json({ method, anonymous: method === "anonymous" }));
    }
    if (path === "/api/v1/profile") {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Partial<Profile>;
        return Promise.resolve(json({ ...profile, ...body }));
      }
      return Promise.resolve(json(profile));
    }
    if (path === "/api/v1/session/secret") {
      return Promise.resolve(secret());
    }
    throw new Error(`the page asked for ${path}`);
  });
  return sent;
}

function field(name: string): HTMLInputElement | HTMLTextAreaElement {
  return document.querySelector(`[name="${name}"]`) as HTMLInputElement;
}

function type(name: string, value: string): void {
  fireEvent.change(field(name), { target: { value } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the settings of the Profile page", () => {
  it("changes the PIN with the current one", async () => {
    const sent = stubRoutes();

    render(<ProfileSettings />);

    expect(await screen.findByText("Your PIN")).toBeDefined();
    type("secret", "1111");
    type("new_secret", "2222");
    type("new_secret_again", "2222");
    fireEvent.click(screen.getByRole("button", { name: "Change PIN" }));

    expect(await screen.findByText("Your PIN is changed.")).toBeDefined();
    expect(sent).toEqual([
      { path: "/api/v1/session/secret", body: { secret: "1111", new_secret: "2222" } },
    ]);
    expect(field("secret").value).toEqual("");
  });

  it("sends nothing when the repeat of the new secret differs", async () => {
    const sent = stubRoutes({ method: "password" });

    render(<ProfileSettings />);

    expect(await screen.findByText("Your password")).toBeDefined();
    type("secret", "old one");
    type("new_secret", "new one");
    type("new_secret_again", "another one");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByText("The new password and its repeat are not the same.")).toBeDefined();
    expect(sent.filter((one) => one.path === "/api/v1/session/secret")).toEqual([]);
  });

  it("says what the viewer said when the change is refused", async () => {
    stubRoutes({ secret: () => json({ error: "That change did not work." }, 403) });

    render(<ProfileSettings />);

    await screen.findByText("Your PIN");
    type("secret", "wrong");
    type("new_secret", "2222");
    type("new_secret_again", "2222");
    fireEvent.click(screen.getByRole("button", { name: "Change PIN" }));

    expect(await screen.findByText("That change did not work.")).toBeDefined();
  });

  it("saves the instructions and the journal consent", async () => {
    const sent = stubRoutes();

    render(<ProfileSettings />);

    expect(await screen.findByDisplayValue("Keep it short.")).toBeDefined();
    type("prompt", "  Call me Al.  ");
    fireEvent.click(screen.getByLabelText(/Ask first/));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved.")).toBeDefined();
    expect(sent).toEqual([{ path: "/api/v1/profile", body: { prompt: "Call me Al.", journal: "ask" } }]);
  });

  it("says that a save overrides the config file for a person it names", async () => {
    stubRoutes({ profile: { ...PROFILE, in_config: true } });

    render(<ProfileSettings />);

    expect(await screen.findByText(/Your name and handles stay as the file sets them/)).toBeDefined();
  });

  it("shows no secret card on an instance with no sign-in", async () => {
    stubRoutes({ method: "anonymous" });

    render(<ProfileSettings />);

    expect(await screen.findByText("Joshua and you")).toBeDefined();
    await vi.waitFor(() => expect(screen.queryByText("Your PIN")).toBeNull());
  });
});

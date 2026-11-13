import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the session gate of the app", () => {
  it("lets a person in after the sign-in, and does not send them back to the form", async () => {
    let signedIn = false;
    const asked: string[] = [];
    vi.stubGlobal("fetch", (path: string) => {
      asked.push(path);
      if (path === "/api/v1/session") {
        return Promise.resolve(
          signedIn
            ? json({ person: { id: "alex", name: "Alex", role: "owner" } })
            : json({ error: "sign in to read this" }, 401),
        );
      }
      if (path === "/api/v1/auth-method") {
        return Promise.resolve(json({ method: "pin", anonymous: false }));
      }
      if (path === "/api/v1/sign-in/people") {
        return Promise.resolve(json({ people: [{ id: "alex", name: "Alex", role: "owner" }] }));
      }
      if (path === "/api/v1/session/sign-in") {
        signedIn = true;
        return Promise.resolve(json({ ok: true }));
      }
      // Every page of the app behind the gate: an empty answer is enough here.
      return Promise.resolve(json({}));
    });

    render(
      <MemoryRouter initialEntries={["/me"]}>
        <App />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("Alex"));
    fireEvent.change(document.querySelector('input[name="secret"]') as HTMLInputElement, {
      target: { value: "4321" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // The app opens and stays open. Before the fix the gate still held the
    // "sign-in" of the first visit, so the browser went straight back to the
    // form, which asked for the list of people a second time.
    await vi.waitFor(() => expect(asked.filter((one) => one === "/api/v1/session").length).toBe(2));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(asked.filter((one) => one === "/api/v1/sign-in/people").length).toBe(1);
    expect(document.querySelector('input[name="secret"]')).toBeNull();
    expect(screen.queryByText("Who is signing in?")).toBeNull();
  });
});

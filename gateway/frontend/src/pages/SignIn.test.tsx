import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import SignIn, { type AuthMethod, type SignInPeople } from "./SignIn";

const PEOPLE: SignInPeople = {
  people: [
    { id: "alex", name: "Alex", role: "owner" },
    { id: "sam", name: "Sam", role: "member" },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The three routes the page calls: the method of the instance, the people of
 * a pin instance, and the sign-in itself. The list it gives back holds the
 * body of each sign-in the page posted.
 */
function stubRoutes(
  method: AuthMethod["method"],
  signedIn: () => Response,
  people: SignInPeople = PEOPLE,
) {
  const sent: unknown[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path === "/api/v1/auth-method") {
      return Promise.resolve(json({ method, anonymous: method === "anonymous" }));
    }
    if (path === "/api/v1/sign-in/people") {
      return Promise.resolve(json(people));
    }
    if (path === "/api/v1/session/sign-in") {
      sent.push(JSON.parse(String(init?.body)));
      return Promise.resolve(signedIn());
    }
    throw new Error(`the page asked for ${path}`);
  });
  return sent;
}

function page() {
  return render(
    <MemoryRouter initialEntries={["/sign-in"]}>
      <Routes>
        <Route path="/sign-in" element={<SignIn />} />
        <Route path="/" element={<p>the wiki</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

/** One field of the form, or null while the form is not on the page. */
function field(name: string): HTMLInputElement | null {
  return document.querySelector(`input[name="${name}"]`);
}

function type(name: string, value: string): void {
  fireEvent.change(field(name) as HTMLInputElement, { target: { value } });
}

function submit(): void {
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the sign-in page", () => {
  it("shows the people of a pin instance, then the form of the one tapped", async () => {
    const sent = stubRoutes("pin", () => json({ ok: true, person: PEOPLE.people[1] }));

    page();

    // The list comes first, and nobody is asked for a secret yet.
    expect(await screen.findByText("Sam")).toBeDefined();
    expect(screen.getByText("Alex")).toBeDefined();
    expect(field("secret")).toBeNull();

    fireEvent.click(screen.getByText("Sam"));

    expect(field("person")?.value).toEqual("sam");
    // The id stays editable, so the person can undo a password manager that
    // wrote its own username into the field.
    expect(field("person")?.readOnly).toBe(false);
    expect(screen.getByText("PIN")).toBeDefined();
    type("secret", "4321");
    fireEvent.click(field("remember") as HTMLInputElement);
    submit();

    expect(await screen.findByText("the wiki")).toBeDefined();
    expect(sent).toEqual([{ person: "sam", secret: "4321", remember: true }]);
  });

  it("signs in with the id the person corrected on a pin instance", async () => {
    const sent = stubRoutes("pin", () => json({ ok: true }));

    page();

    fireEvent.click(await screen.findByText("Sam"));
    type("person", "someone@example.com");
    type("person", "alex");
    type("secret", "4321");
    submit();

    expect(await screen.findByText("the wiki")).toBeDefined();
    expect(sent).toEqual([{ person: "alex", secret: "4321", remember: false }]);
  });

  it("says how to set the first PIN when nobody has one", async () => {
    stubRoutes("pin", () => json({ ok: true }), { people: [] });

    page();

    expect(await screen.findByText("Nobody can sign in yet, because nobody has a PIN.")).toBeDefined();
    expect(screen.getByText(/people auth-reset --id <owner id>/)).toBeDefined();
    expect(screen.getByText(/under Admin → Users/)).toBeDefined();
    expect(field("secret")).toBeNull();
  });

  it("goes back to the list from the form of a pin instance", async () => {
    stubRoutes("pin", () => json({ ok: true }));

    page();

    fireEvent.click(await screen.findByText("Alex"));
    expect(field("person")?.value).toEqual("alex");

    fireEvent.click(screen.getByRole("button", { name: "Not you?" }));

    expect(screen.getByText("Sam")).toBeDefined();
    expect(field("secret")).toBeNull();
  });

  it("asks for the id and the password at once on a password instance", async () => {
    const sent = stubRoutes("password", () => json({ ok: true }));

    page();

    expect(await screen.findByText("Password")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Not you?" })).toBeNull();
    expect(field("person")?.readOnly).toBe(false);
    type("person", "alex");
    type("secret", "a long one");
    submit();

    expect(await screen.findByText("the wiki")).toBeDefined();
    expect(sent).toEqual([{ person: "alex", secret: "a long one", remember: false }]);
  });

  it("says only that the secret did not match when the viewer refuses", async () => {
    stubRoutes("pin", () => json({ error: "That sign-in did not work." }, 401));

    page();

    fireEvent.click(await screen.findByText("Alex"));
    type("secret", "0000");
    submit();

    const line = await screen.findByText("That PIN didn't match. Try again.");
    expect(line.className).toEqual("j-error");
    // The person stays on the form they came from.
    expect(screen.queryByText("the wiki")).toBeNull();
    expect(field("person")?.value).toEqual("alex");
  });

  it("names the password on a password instance, and no PIN", async () => {
    stubRoutes("password", () => json({ error: "That sign-in did not work." }, 401));

    page();

    expect(await screen.findByText("Password")).toBeDefined();
    type("person", "alex");
    type("secret", "nope");
    submit();

    expect(await screen.findByText("That password didn't match. Try again.")).toBeDefined();
  });

  it("says what the viewer said when the sign-in itself is not available", async () => {
    stubRoutes("password", () => json({ error: "Sign-in is not available." }, 503));

    page();

    expect(await screen.findByText("Password")).toBeDefined();
    type("person", "alex");
    type("secret", "a long one");
    submit();

    expect(await screen.findByText("Sign-in is not available.")).toBeDefined();
  });

  it("asks for nothing on an anonymous instance", async () => {
    stubRoutes("anonymous", () => json({ ok: true }));

    page();

    expect(await screen.findByText(/asks nobody to sign in/)).toBeDefined();
    expect(field("secret")).toBeNull();
  });

  it("says what went wrong when the method does not load", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve(json({ error: "forbidden" }, 403)));

    page();

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
  });
});

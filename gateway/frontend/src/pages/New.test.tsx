import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import New, { type NewData } from "./New";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "wiki", label: "Wiki", path: "/wiki/" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const NEW: NewData = {
  folder: "recipes",
  cancel_url: "/wiki/recipes/",
  csrf: "a-token",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * The session, the editor of a new page, and one answer of the save. The list
 * it gives back holds the body of each save the page posted.
 */
function stubRoutes(editor: Response, saved: Response = json({ ok: true, url: "/wiki/x.md" })) {
  const asked: string[] = [];
  const sent: unknown[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    if (path.startsWith("/api/v1/new")) {
      asked.push(path);
      return Promise.resolve(editor);
    }
    if (path === "/api/v1/save") {
      sent.push(JSON.parse(String(init?.body)));
      return Promise.resolve(saved);
    }
    return Promise.resolve(json(SESSION));
  });
  return { asked, sent };
}

function show(query: string) {
  return render(
    <MemoryRouter initialEntries={[`/new${query}`]}>
      <Routes>
        <Route path="/new" element={<New />} />
        <Route path="/wiki/*" element={<p>the new page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a new page of the wiki", () => {
  it("asks for the folder of the address, and names it", async () => {
    const calls = stubRoutes(json(NEW));

    show("?folder=recipes");

    expect(await screen.findByText("new page")).toBeDefined();
    expect(calls.asked).toEqual(["/api/v1/new?folder=recipes"]);
    expect(document.querySelector("p.j-meta")?.textContent).toEqual("in recipes");
    expect(screen.getByText("cancel").getAttribute("href")).toEqual("/wiki/recipes/");
    expect(document.title).toEqual("new page");
  });

  it("says the wiki with no folder in the address", async () => {
    const calls = stubRoutes(json({ ...NEW, folder: "", cancel_url: "/wiki/" }));

    show("");

    expect(await screen.findByText("new page")).toBeDefined();
    expect(calls.asked).toEqual(["/api/v1/new?folder="]);
    expect(document.querySelector("p.j-meta")?.textContent).toEqual("in the wiki");
  });

  it("posts the folder, the name and the text, then goes to the page", async () => {
    const calls = stubRoutes(json(NEW), json({ ok: true, url: "/wiki/recipes/pasta.md" }));

    show("?folder=recipes");

    await screen.findByText("new page");
    fireEvent.change(document.querySelector('input[name="name"]') as HTMLInputElement, {
      target: { value: "pasta.md" },
    });
    fireEvent.change(document.querySelector("textarea") as HTMLTextAreaElement, {
      target: { value: "Flour and eggs.\n" },
    });
    fireEvent.click(screen.getByText("save"));

    expect(await screen.findByText("the new page")).toBeDefined();
    expect(calls.sent).toEqual([
      { csrf: "a-token", folder: "recipes", name: "pasta.md", body: "Flour and eggs.\n" },
    ]);
  });

  it("keeps the editor and says what the viewer said when the name is taken", async () => {
    stubRoutes(json(NEW), json({ error: "A page with that name is already there." }, 409));

    show("?folder=recipes");

    await screen.findByText("new page");
    fireEvent.change(document.querySelector('input[name="name"]') as HTMLInputElement, {
      target: { value: "pizza.md" },
    });
    fireEvent.click(screen.getByText("save"));

    const line = await screen.findByText("A page with that name is already there.");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("textarea")).not.toBeNull();
  });

  it("says what the viewer said when the folder is not there", async () => {
    stubRoutes(json({ error: "not found" }, 404));

    show("?folder=nowhere");

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("textarea")).toBeNull();
  });
});

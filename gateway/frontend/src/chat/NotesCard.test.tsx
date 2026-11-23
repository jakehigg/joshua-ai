import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Workspace } from "../api/workspaces";
import { ASK_PREFIX, NotesCard } from "./NotesCard";

function ws(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w1",
    slug: "solar",
    title: "Solar install",
    owner: "alex",
    goal: "Pick an installer",
    notes: "## Rules\n\n- Keep working.\n\n## Repositories\n\n- old/repo is retired.",
    servers: ["tasks"],
    model: null,
    notify: "none",
    style: "none",
    status: "active",
    shared: false,
    members: [],
    ...over,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("NotesCard", () => {
  it("shows the notes first, under their own name", () => {
    render(<NotesCard workspace={ws()} onSaved={() => undefined} onAsk={() => undefined} />);
    expect(screen.getByRole("region", { name: "Workspace notes" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Rules" })).toBeTruthy();
    expect(screen.getByText("old/repo is retired.")).toBeTruthy();
  });

  it("says so when there are no notes", () => {
    render(<NotesCard workspace={ws({ notes: "" })} onSaved={() => undefined} onAsk={() => undefined} />);
    expect(screen.getByText(/No notes yet/)).toBeTruthy();
  });

  it("folds away and back", () => {
    render(<NotesCard workspace={ws()} onSaved={() => undefined} onAsk={() => undefined} />);
    const toggle = screen.getByRole("button", { name: /Workspace notes/ });
    fireEvent.click(toggle);
    expect(screen.queryByText("old/repo is retired.")).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByText("old/repo is retired.")).toBeTruthy();
  });

  it("saves an edit with one patch of the notes", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        bodies.push({ method: init?.method, body: JSON.parse(String(init?.body)) });
        return new Response(JSON.stringify(ws({ notes: "## Rules\n\n- New rule." })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const saved = vi.fn();
    render(<NotesCard workspace={ws()} onSaved={saved} onAsk={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit notes" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Workspace notes" }), {
      target: { value: "## Rules\n\n- New rule." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save notes" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(bodies).toEqual([{ method: "PATCH", body: { notes: "## Rules\n\n- New rule." } }]);
  });

  it("asks Joshua with one message in the thread", () => {
    const asked = vi.fn();
    render(<NotesCard workspace={ws()} onSaved={() => undefined} onAsk={asked} />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Joshua" }));
    fireEvent.change(screen.getByRole("textbox", { name: "What should change in the notes" }), {
      target: { value: "add that the gitops repo is project 68" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send to Joshua" }));
    expect(asked).toHaveBeenCalledWith(ASK_PREFIX + "add that the gitops repo is project 68");
    expect(screen.getByText("old/repo is retired.")).toBeTruthy();
  });
});

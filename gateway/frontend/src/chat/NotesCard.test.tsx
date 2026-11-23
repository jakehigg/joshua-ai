import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Workspace } from "../api/workspaces";
import { DocumentPanelProvider, ItemSheet, useDocumentPanelState } from "./DocumentPanel";
import { NotesCard, NotesPanel, commentMessage, wholeMessage, type NotesProps } from "./NotesCard";

const NOTES = "## Rules\n\n- Keep working.\n\n## Repositories\n\n- old/repo is retired.";

function ws(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w1",
    slug: "solar",
    title: "Solar install",
    owner: "alex",
    goal: "Pick an installer",
    notes: NOTES,
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

function props(over: Partial<NotesProps> = {}): NotesProps {
  return { workspace: ws(), onSaved: () => undefined, onAsk: () => undefined, ...over };
}

// The card and the sheet as the chat screen holds them: the card opens the
// notes in the sheet.
function Screen({ notes }: { notes: NotesProps }) {
  const panel = useDocumentPanelState();
  return (
    <DocumentPanelProvider panel={panel}>
      <NotesCard workspace={notes.workspace} />
      {panel.view && (
        <ItemSheet
          panel={panel}
          renderNotes={(toggle) => <NotesPanel notes={notes} onClose={panel.close} fullToggle={toggle} />}
        />
      )}
    </DocumentPanelProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("NotesCard", () => {
  it("stands under its own name and opens the notes in the sheet", () => {
    render(<Screen notes={props()} />);
    const card = screen.getByRole("region", { name: "Workspace notes" });
    expect(card.textContent).toContain("Keep working.");
    fireEvent.click(screen.getByRole("button", { name: /Workspace notes/ }));
    const sheet = screen.getByRole("dialog", { name: "Workspace notes" });
    expect(sheet.textContent).toContain("old/repo is retired.");
    expect(screen.getByRole("button", { name: "Full screen" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close the notes" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says so when there are no notes", () => {
    render(<Screen notes={props({ workspace: ws({ notes: "" }) })} />);
    expect(screen.getByText(/No notes yet/)).toBeTruthy();
  });
});

describe("NotesPanel", () => {
  it("sends a comment on one line, with the line it is about", () => {
    const asked = vi.fn();
    render(<NotesPanel notes={props({ onAsk: asked })} onClose={() => undefined} />);
    // The lines that hold text each get a +, and a blank line gets none.
    expect(screen.getAllByRole("button", { name: /Comment on line/ })).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Comment on line 7" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Your comment on this line" }), {
      target: { value: "say which repo replaced it" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send to Joshua" }));
    expect(asked).toHaveBeenCalledWith(commentMessage("- old/repo is retired.", "say which repo replaced it"));
    expect(screen.getByText("Sent to Joshua.")).toBeTruthy();
  });

  it("sends a change for the notes as a whole", () => {
    const asked = vi.fn();
    render(<NotesPanel notes={props({ onAsk: asked })} onClose={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Joshua" }));
    fireEvent.change(screen.getByRole("textbox", { name: "What should change in the notes" }), {
      target: { value: "add the gitops project id" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send to Joshua" }));
    expect(asked).toHaveBeenCalledWith(wholeMessage("add the gitops project id"));
  });

  it("edits the whole text and saves it with one patch", async () => {
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
    render(<NotesPanel notes={props({ onSaved: saved })} onClose={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit notes" }));
    const field = screen.getByRole("textbox", { name: "Workspace notes" });
    expect((field as HTMLTextAreaElement).value).toBe(NOTES);
    expect(screen.getByRole("button", { name: "Save notes" })).toHaveProperty("disabled", true);
    fireEvent.change(field, { target: { value: "## Rules\n\n- New rule." } });
    fireEvent.click(screen.getByRole("button", { name: "Save notes" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(bodies).toEqual([{ method: "PATCH", body: { notes: "## Rules\n\n- New rule." } }]);
  });

  it("opens the editor at once when there are no notes", () => {
    render(<NotesPanel notes={props({ workspace: ws({ notes: "" }) })} onClose={() => undefined} />);
    expect(screen.getByRole("textbox", { name: "Workspace notes" })).toBeTruthy();
  });
});

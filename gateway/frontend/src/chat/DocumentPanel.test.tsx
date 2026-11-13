import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactCard } from "./ArtifactCard";
import {
  COMMENT_HINT,
  DocumentPanel,
  DocumentPanelProvider,
  POLL_MS,
  useDocumentPanelState,
} from "./DocumentPanel";

// A chat screen in small: the conversation with two document cards, and the
// panel beside it, wired the way ChatPage wires them.
function ChatScreen() {
  const panel = useDocumentPanelState();
  return (
    <DocumentPanelProvider panel={panel}>
      <div className="shell">
        <div className="chat">
          <div className="list" data-testid="list">
            <ArtifactCard content="documents/sam/pumpkin-soup.md" />
            <ArtifactCard content="documents/sam/notes.md" />
          </div>
          {/* What a finished turn does with the path of its `artifact`
              fence (useTurn.ts). */}
          <button type="button" onClick={() => panel.reload("documents/sam/pumpkin-soup.md")}>
            wrote the soup
          </button>
          <button type="button" onClick={() => panel.reload("documents/sam/notes.md")}>
            wrote the notes
          </button>
        </div>
        {panel.shown && <DocumentPanel artifact={panel.shown} reloads={panel.reloads} title={panel.title} onClose={panel.close} />}
      </div>
    </DocumentPanelProvider>
  );
}

// The panel header shows the path too, so the card is looked for in the
// conversation alone.
function open(slug: string) {
  const list = screen.getByTestId("list");
  const path = within(list).getByText(`documents/sam/${slug}.md`);
  const card = path.closest(".artifact-card") as HTMLElement;
  return card.querySelector(".artifact-open") as HTMLButtonElement;
}

function frame() {
  return document.querySelector(".doc-panel-frame") as HTMLIFrameElement | null;
}

const FIRST_STAMP = "2026-02-01T10:00:00Z";
// The `updated` the viewer answers with. A change of it is a change made
// outside this chat, by an assignment or from another device.
let stamp = FIRST_STAMP;

// The viewer's JSON route for the two documents: a card reads its title
// from it, and the open panel polls it. Anything else is 404, as the
// document page itself is not asked for here.
function viewer() {
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (!path.endsWith(".json")) return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
    const body = { title: "Pumpkin soup", kind: "artifact", status: "draft", updated: stamp };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

// How often the viewer was asked about one document.
function asks(fetch: ReturnType<typeof viewer>, slug: string) {
  return fetch.mock.calls.filter(([input]) => String(input) === `/documents/sam/${slug}.json`).length;
}

describe("the document panel", () => {
  beforeEach(() => {
    stamp = FIRST_STAMP;
    viewer();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("frames the viewer's own page at a host-absolute path", () => {
    render(<ChatScreen />);
    expect(frame()).toBeNull();
    fireEvent.click(open("pumpkin-soup"));
    const iframe = frame() as HTMLIFrameElement;
    // The src is the viewer's page as the browser loads it, not a route path.
    expect(iframe.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    expect(iframe.getAttribute("srcdoc")).toBeNull();
    expect(iframe.getAttribute("sandbox")).toBeNull();
    expect(iframe.getAttribute("title")).toBe("pumpkin-soup, the document page");
  });

  it("shows the title, a full-page link and the commenting hint as hover text in its header", async () => {
    render(<ChatScreen />);
    fireEvent.click(open("notes"));
    const panel = document.querySelector(".doc-panel") as HTMLElement;
    // Until the viewer answers, the slug; then the title it sent.
    expect(await within(panel).findByText("Pumpkin soup")).toBeTruthy();
    const header = panel.querySelector(".doc-panel-header") as HTMLElement;
    expect(header.getAttribute("title")).toContain("documents/sam/notes.md");
    expect(header.getAttribute("title")).toContain(COMMENT_HINT);
    expect(panel.querySelector(".doc-panel-hint")).toBeNull();
    const full = within(panel).getByRole("link", { name: "Open full page" });
    expect(full.getAttribute("href")).toBe("/documents/sam/notes");
    expect(full.getAttribute("target")).toBe("_blank");
    expect(full.getAttribute("rel")).toBe("noopener noreferrer");
    expect(screen.getByRole("button", { name: "Close the document" })).toBeTruthy();
  });

  it("presses the Open of the document it shows, and only that one", () => {
    render(<ChatScreen />);
    fireEvent.click(open("pumpkin-soup"));
    expect(open("pumpkin-soup").getAttribute("aria-pressed")).toBe("true");
    expect(open("notes").getAttribute("aria-pressed")).toBe("false");
    // One panel at a time: the other card moves it to its own document.
    fireEvent.click(open("notes"));
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/notes?embed=1");
    expect(open("pumpkin-soup").getAttribute("aria-pressed")).toBe("false");
    // Open again closes it.
    fireEvent.click(open("notes"));
    expect(frame()).toBeNull();
  });

  it("closes without remounting the conversation, so the scroll stays", () => {
    render(<ChatScreen />);
    const list = screen.getByTestId("list");
    list.scrollTop = 420;
    fireEvent.click(open("notes"));
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    expect(frame()).toBeNull();
    expect(screen.getByTestId("list")).toBe(list);
    expect(list.scrollTop).toBe(420);
  });

  it("reloads the open frame with a fresh query when that document is written", () => {
    render(<ChatScreen />);
    fireEvent.click(open("pumpkin-soup"));
    const first = frame() as HTMLIFrameElement;
    fireEvent.click(screen.getByRole("button", { name: "wrote the soup" }));
    // A new frame with a new src, not a navigation of the one before.
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
    expect(frame()).not.toBe(first);
    fireEvent.click(screen.getByRole("button", { name: "wrote the soup" }));
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=2");
  });

  it("does not reload for another document, and a reload with no panel does nothing", () => {
    render(<ChatScreen />);
    // No panel: a written document reloads nothing.
    fireEvent.click(screen.getByRole("button", { name: "wrote the soup" }));
    expect(frame()).toBeNull();
    fireEvent.click(open("pumpkin-soup"));
    const first = frame() as HTMLIFrameElement;
    fireEvent.click(screen.getByRole("button", { name: "wrote the notes" }));
    expect(frame()).toBe(first);
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
  });

  it("opens another document fresh after a reload", () => {
    render(<ChatScreen />);
    fireEvent.click(open("pumpkin-soup"));
    fireEvent.click(screen.getByRole("button", { name: "wrote the soup" }));
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
    fireEvent.click(open("notes"));
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/notes?embed=1");
  });

  it("gives the keyboard the close control, which the sheet needs on a phone", () => {
    render(<ChatScreen />);
    fireEvent.click(open("notes"));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close the document" }));
  });
});

// A change made outside this chat — by an assignment, or from another
// device — has no turn to tell of it, so the open panel polls the viewer.
describe("the open panel watching for a change", () => {
  let visible = true;

  beforeEach(() => {
    stamp = FIRST_STAMP;
    visible = true;
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => (visible ? "visible" : "hidden"),
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(document, "visibilityState");
  });

  // Let the answers the panel waits for arrive.
  async function settle() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  // The timer fires `rounds` times, and the answers arrive.
  async function tick(rounds = 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * rounds);
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  async function tab(state: "visible" | "hidden") {
    visible = state === "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  it("reloads the frame when the stamp of the open document changed", async () => {
    const fetch = viewer();
    render(<ChatScreen />);
    await settle();
    // The card asked for its title; nothing polls yet.
    const title = asks(fetch, "pumpkin-soup");
    fireEvent.click(open("pumpkin-soup"));
    await settle();
    // The panel takes the stamp it opened with: one ask, and no reload.
    expect(asks(fetch, "pumpkin-soup")).toBe(title + 1);
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    // The same stamp a round later: still no reload.
    await tick();
    expect(asks(fetch, "pumpkin-soup")).toBe(title + 2);
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    // Somebody else wrote the document.
    stamp = "2026-02-01T11:00:00Z";
    await tick();
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
    // That stamp is the new baseline: it does not reload again.
    await tick(2);
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
    // The panel asked about its own document only.
    expect(asks(fetch, "notes")).toBe(1);
  });

  it("asks nothing more with no panel open, and stops when the panel closes", async () => {
    const fetch = viewer();
    render(<ChatScreen />);
    await settle();
    // The two cards asked for their titles, and that is all: no panel is
    // open, so no timer runs.
    const asked = fetch.mock.calls.length;
    await tick(3);
    expect(fetch.mock.calls.length).toBe(asked);
    fireEvent.click(open("notes"));
    await settle();
    expect(asks(fetch, "notes")).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Close the document" }));
    await tick(3);
    expect(asks(fetch, "notes")).toBe(2);
  });

  it("asks nothing while the tab is hidden, and catches the change as it comes back", async () => {
    const fetch = viewer();
    render(<ChatScreen />);
    await settle();
    fireEvent.click(open("pumpkin-soup"));
    await settle();
    const asked = asks(fetch, "pumpkin-soup");
    await tab("hidden");
    await tick(4);
    expect(asks(fetch, "pumpkin-soup")).toBe(asked);
    // The document changed while the tab was away.
    stamp = "2026-02-01T12:00:00Z";
    await tab("visible");
    // Coming back asks at once, so the change shows with no wait.
    expect(asks(fetch, "pumpkin-soup")).toBeGreaterThan(asked);
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1&r=1");
  });

  it("reloads nothing when the viewer cannot be asked", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ error: "not found" }), { status: 404 }));
    vi.stubGlobal("fetch", fetch);
    render(<ChatScreen />);
    await settle();
    fireEvent.click(open("pumpkin-soup"));
    await settle();
    await tick(3);
    // The frame is the one it opened with, and the card shows its slug.
    expect(frame()?.getAttribute("src")).toBe("/documents/sam/pumpkin-soup?embed=1");
    expect(open("pumpkin-soup").closest(".artifact-card")?.querySelector(".artifact-slug")?.textContent).toBe(
      "pumpkin-soup",
    );
  });
});

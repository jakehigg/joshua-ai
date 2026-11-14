import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "./store";
import { DocumentPanelProvider } from "./DocumentPanel";
import { Message } from "./Message";

function show({ showWho, ...extra }: Partial<ChatMessage> & { showWho?: boolean }) {
  const message: ChatMessage = { id: "m1", role: "joshua", text: "", ts: "2026-01-01T10:00:00Z", state: "ok", ...extra };
  return render(<Message message={message} canRetry onRetry={() => undefined} showWho={showWho} />)
    .container;
}

const ANSWER = "Payback is about **11 years**.\n\n```speech\nAbout eleven years at today's rates.\n```";
const WROTE = "I wrote it down.\n\n```artifact\ndocuments/sam/pumpkin-soup.md\n```";

describe("Message", () => {
  it("prints the answer without an old speech block, and shows no spoken line", () => {
    const c = show({ text: ANSWER });
    expect(c.querySelector("strong")?.textContent).toBe("11 years");
    expect(c.querySelector("pre")).toBeNull();
    expect(c.querySelector(".markdown")?.textContent).not.toContain("eleven");
    expect(c.querySelector(".spoken-line")).toBeNull();
  });

  it("takes the block out of a history row too, and keeps another code block", () => {
    expect(show({ text: ANSWER, serverId: 4 }).querySelector("pre")).toBeNull();
    const c = show({ text: "Plain.\n\n```py\nx = 1\n```" });
    expect(c.querySelector("pre code")).not.toBeNull();
  });

  it("hides a speech fence that is still arriving", () => {
    const c = show({ text: "Answer.\n\n```spe", state: "pending" });
    expect(c.querySelector("pre")).toBeNull();
    expect(c.querySelector(".markdown")?.textContent?.trim()).toBe("Answer.");
  });

  it("shows a document card instead of the artifact block", () => {
    const c = show({ text: WROTE });
    expect(c.querySelector("pre")).toBeNull();
    expect(c.querySelector(".markdown")?.textContent?.trim()).toBe("I wrote it down.");
    expect(c.querySelector(".artifact-slug")?.textContent).toBe("pumpkin-soup");
    expect(c.querySelector(".artifact-path")?.textContent).toBe("documents/sam/pumpkin-soup.md");
  });

  it("shows the card on a history row too, and both blocks of one answer", () => {
    expect(show({ text: WROTE, serverId: 7 }).querySelector(".artifact-card")).not.toBeNull();
    const c = show({ text: `${WROTE}\n\n\`\`\`speech\nI wrote it down.\n\`\`\`` });
    expect(c.querySelector(".artifact-card")).not.toBeNull();
    expect(c.querySelector("pre")).toBeNull();
  });

  it("hides an artifact fence that is still arriving", () => {
    const c = show({ text: "I wrote it down.\n\n```artif", state: "pending" });
    expect(c.querySelector("pre")).toBeNull();
    expect(c.querySelector(".artifact-card")).toBeNull();
    expect(c.querySelector(".artifact-missing")).toBeNull();
    expect(c.querySelector(".markdown")?.textContent?.trim()).toBe("I wrote it down.");
  });

  it("shows one plain line for a path that is not a document path", () => {
    const c = show({ text: "Done.\n\n```artifact\nthe soup recipe\n```" });
    expect(c.querySelector(".artifact-card")).toBeNull();
    expect(c.querySelector(".artifact-missing")?.textContent).toBe(
      "Joshua pointed at a document that is not there",
    );
  });

  it("prints a person message in a bubble, with no voice glyph", () => {
    show({ role: "person", text: "How long?", spoken: true });
    expect(screen.getByText("How long?", { exact: false }).className).toBe("bubble");
    expect(screen.queryByLabelText("sent by voice")).toBeNull();
  });

  it("shows a chip with the file name on a message that carried one", () => {
    const c = show({ role: "person", text: "what is on this?", fileName: "photo.png" });
    expect(c.querySelector(".file-chip")?.textContent).toBe("📎photo.png");
    expect(c.querySelector(".bubble")?.textContent).toContain("what is on this?");
    expect(show({ role: "person", text: "no file" }).querySelector(".file-chip")).toBeNull();
  });

  it("names who wrote a person message when the list asks for the name", () => {
    const named = show({ role: "person", text: "I'll bring pie", who: "Mia", showWho: true });
    expect(named.querySelector(".msg-who")?.textContent).toBe("Mia");
    // The rest of a run from the same person has no line of its own.
    expect(show({ role: "person", text: "and cider", who: "Mia" }).querySelector(".msg-who")).toBeNull();
    // A thread of one person carries no name at all.
    expect(show({ role: "person", text: "hi", showWho: true }).querySelector(".msg-who")).toBeNull();
  });

  it("prints a person message's text as plain text", () => {
    const c = show({ role: "person", text: "```speech\nx\n```" });
    expect(c.querySelector(".bubble")?.textContent).toBe("```speech\nx\n```");
  });
});

describe("a message with a file", () => {
  const base = { id: "m", role: "person" as const, text: "look", ts: "2026-01-01T10:00:00Z", state: "ok" as const };

  it("shows a photo of the history as a thumbnail that opens the image", () => {
    render(
      <Message
        message={{ ...base, attachments: [{ name: "IMG_1.jpg", mime: "image/jpeg", url: "/api/v1/files/wiki/a.jpg" }] }}
        canRetry
        onRetry={() => undefined}
      />,
    );
    const img = screen.getByAltText("IMG_1.jpg");
    expect(img.getAttribute("src")).toBe("/api/v1/files/wiki/a.jpg");
    expect(img.closest("a")?.getAttribute("href")).toBe("/api/v1/files/wiki/a.jpg");
  });

  it("shows a file with no address as a chip that is not a link", () => {
    render(
      <Message
        message={{ ...base, attachments: [{ name: "x.jpg", mime: "image/jpeg" }] }}
        canRetry
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("x.jpg").closest("a")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the photo that was just sent from memory, and revokes it on unmount", () => {
    const create = vi.fn(() => "blob:photo");
    const revoke = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
    const file = new File(["pixels"], "photo.png", { type: "image/png" });
    const { unmount } = render(
      <Message message={{ ...base, fileName: "photo.png", file }} canRetry onRetry={() => undefined} />,
    );
    expect(screen.getByAltText("photo.png").getAttribute("src")).toBe("blob:photo");
    unmount();
    expect(revoke).toHaveBeenCalledWith("blob:photo");
    vi.unstubAllGlobals();
  });

  it("says that a queued turn waits", () => {
    render(
      <Message
        message={{ ...base, role: "joshua", text: "", state: "pending", waiting: true, ts: new Date().toISOString() }}
        canRetry
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("Waiting for Joshua to finish another turn…")).toBeTruthy();
  });
});

describe("a photo in a message", () => {
  const base = { id: "m", role: "person" as const, text: "(file attached — no caption)", ts: "2026-01-01T10:00:00Z", state: "ok" as const };
  const photo = { name: "IMG_1.jpg", mime: "image/jpeg", url: "/api/v1/files/wiki/attachments/a.jpg" };

  it("sits above the bubble with no frame, and a file with no caption has no bubble", () => {
    const { container } = render(
      <Message message={{ ...base, attachments: [photo] }} canRetry onRetry={() => undefined} />,
    );
    expect(container.querySelector(".msg-media .thumb")).not.toBeNull();
    expect(container.querySelector(".bubble")).toBeNull();
  });

  it("keeps the bubble for a real caption", () => {
    const { container } = render(
      <Message message={{ ...base, text: "the rotation", attachments: [photo] }} canRetry onRetry={() => undefined} />,
    );
    expect(container.querySelector(".bubble")?.textContent).toBe("the rotation");
    expect(container.querySelector(".msg-media + .bubble")).not.toBeNull();
  });

  it("opens in the panel of the chat on a tap, and not as a page", () => {
    const open = vi.fn();
    const panel = { shown: null, title: null, reloads: 0, view: null, live: true, show: vi.fn(), open, close: vi.fn(), reload: vi.fn() };
    render(
      <DocumentPanelProvider panel={panel}>
        <Message message={{ ...base, attachments: [photo] }} canRetry onRetry={() => undefined} />
      </DocumentPanelProvider>,
    );
    const link = screen.getByAltText("IMG_1.jpg").closest("a") as HTMLAnchorElement;
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(open).toHaveBeenCalledWith({ type: "image", image: { url: photo.url, title: "IMG_1.jpg" } });
  });

  it("is a plain link outside a chat screen", () => {
    render(<Message message={{ ...base, attachments: [photo] }} canRetry onRetry={() => undefined} />);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    (screen.getByAltText("IMG_1.jpg").closest("a") as HTMLAnchorElement).dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  });
});

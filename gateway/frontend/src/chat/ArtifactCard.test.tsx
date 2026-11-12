import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactCard, MISSING_LINE, parseArtifact } from "./ArtifactCard";

function card(content: string) {
  return render(<ArtifactCard content={content} />).container;
}

// The viewer's JSON route for a document, which the card reads its title
// from. `answer` is given the path the card asked for.
function viewer(answer: (path: string) => Response) {
  const fetch = vi.fn(async (input: RequestInfo | URL) => answer(String(input)));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const META = { title: "Pumpkin soup", kind: "artifact", status: "draft", updated: "2026-02-01T10:00:00Z" };

describe("parseArtifact", () => {
  it("takes the slug and the viewer page of a document path", () => {
    expect(parseArtifact("documents/sam/pumpkin-soup.md")).toEqual({
      path: "documents/sam/pumpkin-soup.md",
      slug: "pumpkin-soup",
      url: "/documents/sam/pumpkin-soup",
    });
    expect(parseArtifact("\ndocuments/sam/notes.md\n")?.slug).toBe("notes");
  });

  it("gives null for anything that is not one such path", () => {
    for (const bad of [
      "",
      "   ",
      "notes.md",
      "documents/notes.md",
      "documents/sam/notes.txt",
      "documents/sam/sub/notes.md",
      "wiki/sam/notes.md",
      "documents//notes.md",
      "documents/sam/.md",
      "documents/../sam/notes.md",
      "documents/sam/my notes.md",
      "documents/sam/a.md\ndocuments/sam/b.md",
      '{"path": "documents/sam/notes.md"}',
    ]) {
      expect(parseArtifact(bad), bad).toBeNull();
    }
  });
});

describe("ArtifactCard", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the document's title, the path, and Open", async () => {
    const fetch = viewer(() => json(META));
    const c = card("documents/sam/pumpkin-soup.md");
    // The slug stands there until the title has arrived.
    expect(c.querySelector(".artifact-slug")?.textContent).toBe("pumpkin-soup");
    await waitFor(() => expect(c.querySelector(".artifact-slug")?.textContent).toBe("Pumpkin soup"));
    // The viewer's own route, outside this app's /chat base path.
    expect(fetch).toHaveBeenCalledWith("/documents/sam/pumpkin-soup.json", expect.anything());
    expect(c.querySelector(".artifact-path")?.textContent).toBe("documents/sam/pumpkin-soup.md");
    expect(c.querySelector(".artifact-open")?.textContent).toBe("Open");
    expect(c.querySelector(".artifact-missing")).toBeNull();
  });

  it("keeps the card as it is when the title cannot be read", async () => {
    for (const answer of [
      () => json({ error: "not found" }, 404),
      () => json({ ...META, title: "  " }),
      () => new Response("not json", { status: 200 }),
      () => {
        throw new TypeError("Failed to fetch");
      },
    ]) {
      const fetch = viewer(answer);
      const c = card("documents/sam/pumpkin-soup.md");
      await waitFor(() => expect(fetch).toHaveBeenCalled());
      expect(c.querySelector(".artifact-slug")?.textContent).toBe("pumpkin-soup");
      expect(c.querySelector(".artifact-path")?.textContent).toBe("documents/sam/pumpkin-soup.md");
      expect(c.textContent).not.toContain("not found");
      vi.unstubAllGlobals();
    }
  });

  // The panel belongs to the chat screen; see DocumentPanel.test.tsx.
  it("opens nothing, and stays unpressed, with no panel around it", () => {
    viewer(() => json({ error: "not found" }, 404));
    const c = card("documents/sam/notes.md");
    const open = c.querySelector(".artifact-open") as HTMLButtonElement;
    expect(open.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(open);
    expect(open.getAttribute("aria-pressed")).toBe("false");
    expect(c.querySelector("iframe")).toBeNull();
  });

  it("shows one plain line, and no card, for a path that is not there", () => {
    const fetch = viewer(() => json(META));
    const c = card("the recipe");
    expect(c.querySelector(".artifact-missing")?.textContent).toBe(MISSING_LINE);
    expect(c.querySelector(".artifact-card")).toBeNull();
    expect(c.querySelector("button")).toBeNull();
    // There is no document to ask about.
    expect(fetch).not.toHaveBeenCalled();
  });
});

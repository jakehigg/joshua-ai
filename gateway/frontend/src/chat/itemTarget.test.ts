import { describe, expect, it } from "vitest";
import type { WorkspaceItem } from "../api/workspaces";
import { isImageItem, itemTarget } from "./itemTarget";

function item(over: Partial<WorkspaceItem>): WorkspaceItem {
  return {
    id: "i",
    kind: "link",
    ref: "x",
    title: "T",
    url: null,
    added_by: "tool:x",
    pinned: false,
    seen: true,
    ...over,
  };
}

describe("itemTarget", () => {
  it("opens a document by its ref, with or without .md or .html", () => {
    for (const ref of ["documents/jake/smoke-test-note.md", "documents/jake/smoke-test-note"]) {
      expect(itemTarget(item({ kind: "document", ref }))).toEqual({
        type: "document",
        artifact: {
          path: "documents/jake/smoke-test-note.md",
          slug: "smoke-test-note",
          url: "/documents/jake/smoke-test-note",
        },
      });
    }
  });

  it("keeps .html for an artifact, from its ref or its kind", () => {
    expect(itemTarget(item({ kind: "artifact", ref: "documents/jake/dining-room-panel-mockup.html", url: "/documents/jake/dining-room-panel-mockup" }))).toEqual({
      type: "document",
      artifact: {
        path: "documents/jake/dining-room-panel-mockup.html",
        slug: "dining-room-panel-mockup",
        url: "/documents/jake/dining-room-panel-mockup",
      },
    });
    const byUrl = itemTarget(item({ kind: "artifact", ref: "odd", url: "/documents/jake/x" }));
    expect(byUrl).toMatchObject({ type: "document", artifact: { path: "documents/jake/x.html" } });
  });

  it("opens a document by its url when the ref says nothing", () => {
    const target = itemTarget(item({ kind: "artifact", ref: "odd", url: "/documents/jake/smoke-test-note" }));
    expect(target?.type).toBe("document");
  });

  it("frames another viewer path as a page", () => {
    expect(itemTarget(item({ kind: "wiki", ref: "w", title: "Menu", url: "/wiki/menu.md" }))).toEqual({
      type: "page",
      page: { url: "/wiki/menu.md", title: "Menu", kindLabel: "Wiki page" },
    });
    expect(itemTarget(item({ kind: "assignment", ref: "7", url: null }))).toMatchObject({
      type: "page",
      page: { url: "/assignments/7" },
    });
  });

  it("shows a card with an external button for an http(s) url", () => {
    expect(itemTarget(item({ kind: "pr", ref: "r", title: "Fix", url: "https://github.com/a/b/pull/1" }))).toEqual({
      type: "card",
      card: {
        title: "Fix",
        kindLabel: "Pull request",
        address: "https://github.com/a/b/pull/1",
        external: "https://github.com/a/b/pull/1",
      },
    });
  });

  it("shows a card for an item with no url and no viewer page", () => {
    expect(itemTarget(item({ kind: "tasks_project", ref: "trip-plan", title: "Trip" }))).toEqual({
      type: "card",
      card: { title: "Trip", kindLabel: "Task project", address: "trip-plan", external: null },
    });
  });

  it("gives no target for an unsafe url", () => {
    expect(itemTarget(item({ url: "javascript:alert(1)" }))).toBeNull();
    expect(itemTarget(item({ url: "//evil.test/x" }))).toBeNull();
  });
});

describe("a photo item", () => {
  const url = "/api/v1/files/wiki/attachments/2026/11/epaper.jpg";

  it("opens as an image in the panel", () => {
    expect(itemTarget(item({ kind: "file", ref: "wiki/attachments/2026/11/epaper.jpg", title: "Display", url }))).toEqual({
      type: "image",
      image: { url, title: "Display" },
    });
    expect(isImageItem(item({ kind: "file", ref: "x.PNG", url }))).toBe(true);
  });

  it("is not an image without a viewer path, or for another kind, or another file type", () => {
    expect(isImageItem(item({ kind: "file", ref: "a.jpg", url: null }))).toBe(false);
    expect(isImageItem(item({ kind: "file", ref: "a.jpg", url: "https://x.test/a.jpg" }))).toBe(false);
    expect(isImageItem(item({ kind: "link", ref: "a.jpg", url }))).toBe(false);
    expect(isImageItem(item({ kind: "file", ref: "bill.pdf", url: "/api/v1/files/wiki/bill.pdf" }))).toBe(false);
  });
});

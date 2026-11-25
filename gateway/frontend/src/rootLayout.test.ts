import { describe, expect, it } from "vitest";
// Read from disk: a CSS import gives no text in the test environment. The
// app has no Node types, so node:fs comes in untyped, as in buildStamp.ts.
const node = (name: string): Promise<any> => import(/* @vite-ignore */ name);
const { readFileSync } = await node("node:fs");
const read = (path: string): string =>
  readFileSync(new URL(path, import.meta.url), "utf8") as string;
const indexCss = read("./index.css");
const viewerCss = read("./viewer.css");
const sharedCss = read("../../joshua_gateway/ui/joshua-ui.css");

// The selectors and bodies of every rule whose selector list names :root or
// html alone (not html.<class> or html:has(...)).
function rootRules(css: string): string[] {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: string[] = [];
  for (const match of bare.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const [, head = "", body = ""] = match;
    const selectors = head.split(",").map((part) => part.trim());
    if (selectors.some((sel) => sel === ":root" || sel === "html")) out.push(body);
  }
  return out;
}

describe("the root element", () => {
  it("is never a flex or grid container, so a page in a frame fills its width", () => {
    expect(rootRules(indexCss).length).toBeGreaterThan(0);
    for (const css of [sharedCss, indexCss, viewerCss]) {
      for (const body of rootRules(css)) {
        expect(body).not.toMatch(/(^|;|\s)display\s*:/);
        expect(body).not.toMatch(/(^|;|\s)flex\s*:/);
      }
    }
  });

  it("leaves the layout of the chat to the chat screen", () => {
    expect(indexCss).toMatch(/\.chat-screen\s*\{[^}]*display:\s*flex/);
  });
});

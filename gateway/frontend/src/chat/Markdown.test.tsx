import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Markdown } from "./Markdown";

function md(text: string) {
  return render(<Markdown text={text} />).container;
}

describe("Markdown", () => {
  afterEach(cleanup);

  it("renders a GFM table inside a scroll box", () => {
    const c = md("| a | b |\n|---|---|\n| 1 | 2 |\n");
    expect(c.querySelector(".table-wrap table")).not.toBeNull();
    expect(c.querySelectorAll("td")).toHaveLength(2);
  });

  it("renders task lists and strikethrough", () => {
    const c = md("- [x] done\n- [ ] open\n\n~~gone~~");
    expect(c.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
    expect(c.querySelector("del")?.textContent).toBe("gone");
  });

  it("highlights a fenced code block and gives it a Copy button", () => {
    const c = md("```python\ndef f():\n    return 1\n```");
    const code = c.querySelector("pre code");
    expect(code?.className).toContain("hljs");
    expect(code?.className).toContain("language-python");
    expect(code?.querySelector("span.hljs-keyword")).not.toBeNull();
    expect(c.querySelector(".code-block button")?.textContent).toBe("Copy");
  });

  it("renders a speech block as nothing", () => {
    const c = md("Hi.\n\n```Speech\nSaid.\n```\n\n```js\nx\n```");
    expect(c.querySelectorAll("pre")).toHaveLength(1);
    expect(c.textContent).not.toContain("Said.");
  });

  it("renders an artifact block as nothing", () => {
    const c = md("Hi.\n\n```Artifact\ndocuments/sam/notes.md\n```\n\n```js\nx\n```");
    expect(c.querySelectorAll("pre")).toHaveLength(1);
    expect(c.textContent).not.toContain("documents/sam/notes.md");
  });

  it("copies the code text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const c = md("```\nls -la\n```");
    fireEvent.click(c.querySelector(".code-block button") as HTMLElement);
    expect(writeText).toHaveBeenCalledWith("ls -la\n");
  });

  it("does not throw with no clipboard", () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const c = md("```\nx\n```");
    expect(() => fireEvent.click(c.querySelector(".code-block button") as HTMLElement)).not.toThrow();
  });

  it("strips script elements, event handlers, and javascript: links", () => {
    const c = md(
      '<script>alert(1)</script>\n\n<img src="x" onerror="alert(1)">\n\n[click](javascript:alert(1))\n\n<a href="#" onclick="alert(1)">a</a>',
    );
    expect(c.querySelector("script")).toBeNull();
    expect(c.innerHTML).not.toMatch(/onerror|onclick/);
    expect(c.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("opens links in a new tab, with no opener", () => {
    const c = md("[docs](https://example.net/docs) and https://example.org");
    const links = c.querySelectorAll("a");
    expect(links).toHaveLength(2);
    for (const a of links) {
      expect(a.getAttribute("target")).toBe("_blank");
      expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });
});

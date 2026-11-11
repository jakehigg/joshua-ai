import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Markdown, { wikiRewriter } from "./Markdown";

const TABLE = ["| day | meal |", "| --- | ---- |", "| mon | soup |"].join("\n");

const TASKS = ["- [x] buy rice", "- [ ] cook it"].join("\n");

function clipboard(): ReturnType<typeof vi.fn> {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  return writeText;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the Markdown renderer", () => {
  it("wraps the output in the prose article", () => {
    render(<Markdown text="hello" />);

    const article = document.querySelector("article.j-prose");
    expect(article).not.toBeNull();
    expect(article?.textContent).toEqual("hello");
  });

  it("renders a GFM table", () => {
    render(<Markdown text={TABLE} />);

    expect(document.querySelectorAll("table").length).toEqual(1);
    expect(document.querySelectorAll("tbody tr").length).toEqual(1);
    expect(screen.getByText("soup")).toBeDefined();
  });

  it("renders a task list", () => {
    render(<Markdown text={TASKS} />);

    const boxes = document.querySelectorAll('input[type="checkbox"]');
    expect(boxes.length).toEqual(2);
    expect((boxes[0] as HTMLInputElement).checked).toEqual(true);
    expect((boxes[1] as HTMLInputElement).checked).toEqual(false);
  });

  it("drops the HTML in a page", () => {
    render(<Markdown text={'<script>alert(1)</script>\n\n<b>bold</b>\n\nplain'} />);

    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
    expect(screen.getByText("plain")).toBeDefined();
  });

  it("points an attachment of a page at the route that delivers it", () => {
    render(
      <Markdown
        text={"![a](/attachments/2026/11/a.jpg)\n\n[the file](/attachments/2026/11/a.pdf)"}
        rewriteUrl={wikiRewriter("/wiki/recipes/")}
      />,
    );

    expect(document.querySelector("img")?.getAttribute("src")).toEqual(
      "/api/v1/files/wiki/attachments/2026/11/a.jpg",
    );
    expect(screen.getByText("the file").getAttribute("href")).toEqual(
      "/api/v1/files/wiki/attachments/2026/11/a.pdf",
    );
  });

  it("resolves a relative link against the folder of the page", () => {
    render(
      <Markdown
        text={
          "[sauce](sauce.md) [up](../index.md) [page](/wiki/Home.md) [out](https://example.com/)"
        }
        rewriteUrl={wikiRewriter("/wiki/recipes/")}
      />,
    );

    expect(screen.getByText("sauce").getAttribute("href")).toEqual("/wiki/recipes/sauce.md");
    expect(screen.getByText("up").getAttribute("href")).toEqual("/wiki/index.md");
    expect(screen.getByText("page").getAttribute("href")).toEqual("/wiki/Home.md");
    expect(screen.getByText("out").getAttribute("href")).toEqual("https://example.com/");
  });

  it("leaves a link that goes above the wiki as it is", () => {
    render(<Markdown text="[out](../../etc/passwd)" rewriteUrl={wikiRewriter("/wiki/")} />);

    expect(screen.getByText("out").getAttribute("href")).toEqual("../../etc/passwd");
  });

  it("copies the text of a code block", () => {
    const writeText = clipboard();
    render(<Markdown text={"```\nmake test\n```"} />);

    const button = screen.getByRole("button", { name: "copy" });
    fireEvent.click(button);

    expect(writeText).toHaveBeenCalledWith("make test\n");
    expect(screen.getByRole("button", { name: "copied" })).toBeDefined();
  });
});

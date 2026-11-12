import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import Progress, { Notice, documentUrl, isUrl } from "./Progress";
import type { Checkpoint, Notice as NoticeData, Output } from "./data";

const CHECKPOINTS: Checkpoint[] = [
  { at: "2026-11-09T14:00:00+00:00", when: "1h ago", note: "Read the pages." },
  { at: "2026-11-09T12:00:00+00:00", when: "3h ago", note: "Started." },
];

const OUTPUTS: Output[] = [
  {
    kind: "document",
    ref: "documents/alex/party.md",
    title: "Party plan",
    when: "1h ago",
  },
  { kind: "document", ref: "documents/shared/meals.md", title: "Meal plan", when: "" },
  { kind: "link", ref: "https://example.com/page", title: "The page", when: "2h ago" },
  { kind: "", ref: "a-thing", title: "", when: "" },
];

const REQUEST: NoticeData = {
  kind: "request",
  heading: "The assignment asks for a tool",
  when: "5m ago",
  ref: "search_web",
  text: "Reason: to read the page",
  foot: "Joshua grants it, provides it, or refuses it.",
};

function page(node: ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

describe("what an output points at", () => {
  it("knows an address of the web", () => {
    expect(isUrl("https://example.com/page")).toBe(true);
    expect(isUrl("file:///etc/passwd")).toBe(false);
    expect(isUrl("documents/alex/party.md")).toBe(false);
  });

  it("knows the page of a document, and nothing of another path", () => {
    expect(documentUrl("documents/alex/party.md")).toEqual("/documents/alex/party");
    expect(documentUrl("documents/shared/meals.md")).toEqual("/documents/shared/meals");
    expect(documentUrl("wiki/party.md")).toBeNull();
    expect(documentUrl("documents/alex/party")).toBeNull();
  });
});

describe("the notice of an assignment", () => {
  it("gives the heading, when it asked, the reference and the reason", () => {
    page(<Notice notice={REQUEST} />);

    const notice = document.querySelector("aside.a-notice.warn") as HTMLElement;
    expect(notice.querySelector("strong")?.textContent).toEqual(
      "The assignment asks for a tool 5m ago",
    );
    expect(notice.querySelector("code")?.textContent).toEqual("search_web");
    expect(screen.getByText("Reason: to read the page")).toBeDefined();
    expect(screen.getByText("Joshua grants it, provides it, or refuses it.")).toBeDefined();
  });

  it("gives nothing when the assignment waits for nothing", () => {
    page(<Notice notice={null} />);

    expect(document.querySelector("aside.a-notice")).toBeNull();
  });
});

describe("the progress of an assignment", () => {
  it("groups the outputs by kind, and opens each one where it points", () => {
    page(<Progress outputs={OUTPUTS} checkpoints={CHECKPOINTS} done={false} />);

    expect(screen.getByText("Outputs so far")).toBeDefined();
    expect([...document.querySelectorAll(".a-group .j-label")].map((i) => i.textContent)).toEqual([
      "document 2",
      "link 1",
      "other 1",
    ]);
    const first = document.querySelector(".a-outs .a-out") as HTMLElement;
    expect(first.querySelector("a.a-out-title")?.getAttribute("href")).toEqual(
      "/documents/alex/party",
    );
    expect(first.querySelector("code.a-out-ref")?.textContent).toEqual("documents/alex/party.md");
    expect(first.querySelector(".a-out-note")?.textContent).toEqual("1h ago");
    const link = screen.getByText("The page");
    expect(link.getAttribute("href")).toEqual("https://example.com/page");
    expect(screen.getByText("example.com")).toBeDefined();
    expect(screen.getByText("a-thing").tagName).toEqual("CODE");
  });

  it("names the outputs of an assignment that is done", () => {
    page(<Progress outputs={OUTPUTS} checkpoints={[]} done />);

    expect(screen.getByText("Outputs")).toBeDefined();
  });

  it("gives the checkpoints, the newest first", () => {
    page(<Progress outputs={[]} checkpoints={CHECKPOINTS} done={false} />);

    const notes = [...document.querySelectorAll("ol.a-timeline li")];
    expect(notes.map((item) => item.querySelector(".a-text")?.textContent)).toEqual([
      "Read the pages.",
      "Started.",
    ]);
    expect(notes[0]?.querySelector(".a-at")?.textContent).toEqual("1h ago");
    expect(document.querySelector("#outputs")).toBeNull();
  });

  it("says there is no checkpoint yet when the worker wrote none", () => {
    page(<Progress outputs={[]} checkpoints={[]} done={false} />);

    expect(screen.getByText("No checkpoint yet.")).toBeDefined();
  });
});

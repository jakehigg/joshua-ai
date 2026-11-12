import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import OutputCard, { KIND_ICONS, external, host } from "./OutputCard";
import type { ReportOutput } from "../assignments/data";

function output(fields: Partial<ReportOutput> = {}): ReportOutput {
  return {
    kind: "page",
    icon: "page",
    ref: "wiki/party.md",
    title: "The party page",
    note: "",
    url: null,
    line: 1,
    first_line: 10,
    last_line: 10,
    ...fields,
  };
}

describe("one output of a report", () => {
  it("gives the icon of its kind", () => {
    render(<OutputCard output={output({ icon: "issue" })} />);

    const icon = document.querySelector("svg.j-icon.a-out-icon") as SVGElement;
    expect(icon.getAttribute("aria-hidden")).toEqual("true");
    expect(icon.getAttribute("viewBox")).toEqual("0 0 24 24");
    expect(icon.querySelectorAll("circle").length).toEqual(2);
  });

  it("falls back to the icon of another kind", () => {
    render(<OutputCard output={output({ icon: "nothing-like-it" })} />);

    const path = document.querySelector("svg path")?.getAttribute("d") ?? "";
    expect(KIND_ICONS["other"]).toContain(path);
  });

  it("links a ref that is a URL, and names its host", () => {
    render(
      <OutputCard
        output={output({
          kind: "issue",
          icon: "issue",
          ref: "https://git.example.com/joshua/issues/12",
          title: "Issue 12",
          url: "https://git.example.com/joshua/issues/12",
        })}
      />,
    );

    const link = screen.getByRole("link", { name: "Issue 12" });
    expect(link.getAttribute("href")).toEqual("https://git.example.com/joshua/issues/12");
    expect(link.getAttribute("rel")).toEqual("noreferrer");
    expect(document.querySelector("span.a-out-ref")?.textContent).toEqual("git.example.com");
  });

  it("links a page of the viewer, and gives its ref as code", () => {
    render(
      <OutputCard
        output={output({
          kind: "document",
          icon: "page",
          ref: "documents/alex/party.md",
          title: "Party plan",
          url: "/documents/alex/party",
        })}
      />,
    );

    expect(screen.getByRole("link", { name: "Party plan" }).getAttribute("href")).toEqual(
      "/documents/alex/party",
    );
    expect(document.querySelector("code.a-out-ref")?.textContent).toEqual(
      "documents/alex/party.md",
    );
  });

  it("gives a title with no link as text, and its ref as code", () => {
    render(<OutputCard output={output()} />);

    expect(document.querySelector("a")).toBeNull();
    expect(document.querySelector("span.a-out-title")?.textContent).toEqual("The party page");
    expect(document.querySelector("code.a-out-ref")?.textContent).toEqual("wiki/party.md");
  });

  it("gives the ref alone when the output has no title", () => {
    render(<OutputCard output={output({ title: "" })} />);

    expect(document.querySelector("code.a-out-title")?.textContent).toEqual("wiki/party.md");
    expect(document.querySelector(".a-out-ref")).toBeNull();
  });

  it("gives the note under the title", () => {
    render(<OutputCard output={output({ note: "Twenty people come." })} />);

    expect(document.querySelector(".a-out-note")?.textContent).toContain("Twenty people come.");
  });

  it("knows a place on the internet from a page of the viewer", () => {
    expect(external("https://example.com/a")).toBe(true);
    expect(external("/documents/alex/party")).toBe(false);
    expect(host("https://example.com/a")).toEqual("example.com");
    expect(host("documents/alex/party.md")).toEqual("");
  });
});

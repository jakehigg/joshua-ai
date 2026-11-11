import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CommentCard, { ago, where, type DocComment } from "./CommentCard";

/** The clock of every test here, so the relative times are fixed. */
const NOW = new Date("2026-11-09T15:00:00Z");

const COMMENT: DocComment = {
  id: "3",
  line_no: 5,
  line_text: "- Order the food",
  line_now: "- Order the food",
  stale: false,
  body: "Ask the baker first.",
  author: "alex",
  created_at: "2026-11-09T12:00:00+00:00",
  resolved_at: null,
  resolution: null,
};

/** What the page gives a card beside the comment itself. */
interface CardProps {
  canDelete?: boolean;
  quote?: boolean;
  onDelete?: (id: string) => void;
}

function card(comment: Partial<DocComment>, props: CardProps = {}) {
  vi.spyOn(Date, "now").mockReturnValue(NOW.getTime());
  return render(<CommentCard comment={{ ...COMMENT, ...comment }} {...props} />);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the relative time", () => {
  it("follows the rule of the viewer", () => {
    expect(ago(0)).toEqual("just now");
    expect(ago(-10)).toEqual("just now");
    expect(ago(59)).toEqual("just now");
    expect(ago(60)).toEqual("1m ago");
    expect(ago(59 * 60)).toEqual("59m ago");
    expect(ago(3 * 3600)).toEqual("3h ago");
    expect(ago(47 * 3600)).toEqual("47h ago");
    expect(ago(48 * 3600)).toEqual("2d ago");
    expect(ago(59 * 24 * 3600)).toEqual("59d ago");
    expect(ago(60 * 24 * 3600)).toEqual("2mo ago");
  });

  it("names the line of a comment, and line 0 as the document", () => {
    expect(where(0)).toEqual("The document");
    expect(where(12)).toEqual("Line 12");
  });
});

describe("a comment card", () => {
  it("gives the author, the time and the body", () => {
    card({});

    expect(screen.getByText("alex")).toBeDefined();
    expect(screen.getByText("Ask the baker first.")).toBeDefined();
    const when = document.querySelector("time") as HTMLTimeElement;
    expect(when.textContent).toEqual("3h ago");
    expect(when.getAttribute("datetime")).toEqual("2026-11-09T12:00:00+00:00");
    expect(when.getAttribute("title")).not.toEqual("");
    expect(document.querySelector(".a-comment")?.id).toEqual("C3");
  });

  it("marks a comment whose line changed, and says what it was on", () => {
    card({ stale: true, line_now: "- Order the cake" });

    expect(document.querySelector(".a-comment.stale")).not.toBeNull();
    expect(screen.getByText("line changed")).toBeDefined();
    expect(screen.getByText("It was on:")).toBeDefined();
    expect(screen.getByText("- Order the food")).toBeDefined();
  });

  it("quotes the line when the comment is not on the first line of its unit", () => {
    card({}, { quote: true });

    expect(screen.getByText("Line 5:")).toBeDefined();
    expect(document.querySelector(".a-was code")?.textContent).toEqual("- Order the food");
  });

  it("gives the resolution of Joshua on a resolved comment, and no Delete", () => {
    card(
      { resolved_at: "2026-11-09T14:00:00+00:00", resolution: "The baker is booked." },
      { canDelete: true, onDelete: () => undefined },
    );

    expect(document.querySelector(".a-comment.resolved")).not.toBeNull();
    expect(screen.getByText("Joshua")).toBeDefined();
    expect(screen.getByText("The baker is booked.")).toBeDefined();
    expect(screen.getByText("Line 5:")).toBeDefined();
    expect(screen.queryByText("Delete")).toBeNull();
  });

  it("gives Delete on an open comment, and sends its id", () => {
    const removed: string[] = [];

    card(
      {},
      {
        canDelete: true,
        onDelete: (id: string) => {
          removed.push(id);
        },
      },
    );
    fireEvent.click(screen.getByText("Delete"));

    expect(removed).toEqual(["3"]);
  });

  it("gives no Delete on a comment of somebody else", () => {
    card({}, { canDelete: false, onDelete: () => undefined });

    expect(screen.queryByText("Delete")).toBeNull();
  });

  it("gives no time when there is none", () => {
    card({ created_at: null });

    expect(document.querySelector("time")).toBeNull();
  });
});

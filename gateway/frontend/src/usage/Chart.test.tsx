import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Chart, { EMPTY_TEXT } from "./Chart";
import type { UsageBody, UsageCounts } from "./data";

const TODAY = "2026-11-07";

function counts(total: number): UsageCounts {
  return {
    calls: 1,
    input: total,
    output: 0,
    cache_read: 0,
    cache_write: 0,
    total,
    models: [],
  };
}

const BODY: UsageBody = {
  timezone: "America/New_York",
  days: 30,
  groups: ["chat", "extra"],
  rows: [
    {
      date: "2026-11-07",
      groups: {
        chat: { ...counts(120), workers: {} },
        extra: { ...counts(7900), workers: {} },
      },
    },
    { date: "2026-11-01", groups: { chat: { ...counts(300), workers: {} } } },
  ],
  totals: {
    chat: { ...counts(420), workers: {} },
    extra: { ...counts(7900), workers: {} },
  },
};

function chart(body: UsageBody = BODY) {
  return render(<Chart body={body} today={TODAY} />);
}

function bars(): Element[] {
  return [...document.querySelectorAll("rect")];
}

function axis(): string[] {
  return [...document.querySelectorAll("text.axis")].map((item) => item.textContent ?? "");
}

describe("the chart of the tokens of each day", () => {
  it("stacks one segment for each group of a day, in the order of the groups", () => {
    chart();

    // The days run oldest first, and the groups of a day in their own order.
    expect(bars().length).toEqual(3);
    expect(bars().map((bar) => bar.getAttribute("class"))).toEqual(["s0", "s0", "s1"]);
    const [first, chat, extra] = bars();
    expect(first?.querySelector("title")?.textContent).toEqual("2026-11-01 · chat: 300 tokens");
    // A segment is as tall as its share of the tallest day of the window.
    expect(chat?.getAttribute("height")).toEqual("2.7");
    expect(extra?.getAttribute("y")).toEqual("12.0");
    expect(chat?.querySelector("title")?.textContent).toEqual("2026-11-07 · chat: 120 tokens");
    expect(extra?.querySelector("title")?.textContent).toEqual(
      "2026-11-07 · extra: 7 900 tokens",
    );
  });

  it("spans the window of the answer, with a slot for each day", () => {
    chart();

    const svg = document.querySelector("svg");
    expect(svg?.getAttribute("viewBox")).toEqual("0 0 640 220");
    expect(svg?.getAttribute("aria-label")).toEqual("Total tokens per day, stacked by group");
    // 30 days, so a bar is four fifths of a thirtieth of the plot.
    expect(bars()[0]?.getAttribute("width")).toEqual("15.6");
    // The window ends today and starts 29 days before it.
    expect(axis()).toContain("2026-10-09");
    expect(axis()).toContain(TODAY);
    expect(axis().slice(0, 3)).toEqual(["0", "4k", "8k"]);
  });

  it("draws the legend of each group with the class of its colour", () => {
    chart();

    const items = [...document.querySelectorAll("ul.legend li")];
    expect(items.map((item) => item.textContent)).toEqual(["chat", "extra"]);
    expect(items[0]?.querySelector("span")?.className).toEqual("sw s0");
    expect(items[1]?.querySelector("span")?.className).toEqual("sw s1");
    expect(document.querySelector("figure")?.className).toEqual("j-card usage-chart");
    expect(document.querySelector("[style]")).toBeNull();
  });

  it("says that a window with no usage is empty, and still shows the window", () => {
    chart({ timezone: "UTC", days: 90, groups: [], rows: [], totals: {} });

    expect(screen.getByText(EMPTY_TEXT)).toBeDefined();
    expect(bars()).toEqual([]);
    expect(axis()).toContain("2026-08-10");
    expect(axis()).toContain(TODAY);
  });

  it("draws no bar for a day outside the window", () => {
    chart({ ...BODY, days: 1 });

    // Only today is in a one-day window: the day of the 1st is left out.
    expect(bars().length).toEqual(2);
    expect(axis()).toContain(TODAY);
    expect(axis()).not.toContain("2026-11-01");
  });
});

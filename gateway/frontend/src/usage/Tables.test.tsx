import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Tables, { NO_MODELS } from "./Tables";
import type { UsageBody, UsageCounts, UsageGroup } from "./data";

function counts(calls: number, total: number, models: string[] = []): UsageCounts {
  return {
    calls,
    input: total,
    output: 0,
    cache_read: 0,
    cache_write: 0,
    total,
    models,
  };
}

function group(calls: number, total: number, models: string[] = []): UsageGroup {
  return { ...counts(calls, total, models), workers: {} };
}

const EXTRA: UsageGroup = {
  ...counts(5, 7900, ["m-editor", "m-lookup"]),
  workers: {
    editor: counts(1, 1800, ["m-editor"]),
    internet: counts(4, 6100, ["m-lookup"]),
  },
};

const BODY: UsageBody = {
  timezone: "America/New_York",
  days: 30,
  groups: ["chat", "reflection", "extra"],
  rows: [
    {
      date: "2026-11-07",
      groups: { chat: group(3, 120, ["m-chat"]), extra: EXTRA },
    },
    { date: "2026-11-01", groups: { chat: group(1, 300, ["m-chat"]) } },
  ],
  totals: { chat: group(4, 420, ["m-chat"]), extra: EXTRA },
};

function tables(body: UsageBody = BODY) {
  return render(<Tables body={body} />);
}

/** The table under a heading, as rows of cell texts. */
function table(heading: string): string[][] {
  const title = screen.getByText(heading);
  const wrap = title.nextElementSibling as HTMLElement;
  expect(wrap.className).toEqual("j-table-wrap");
  expect(wrap.querySelector("table")?.className).toEqual("j-table usage");
  return [...wrap.querySelectorAll("tr")].map((row) =>
    [...row.querySelectorAll("th, td")].map((cell) => cell.textContent ?? ""),
  );
}

describe("the tables of the usage", () => {
  it("shows the days down and the groups across, with a total of each one", () => {
    tables();

    const rows = table("Tokens by day");
    expect(rows[0]).toEqual(["Date", "chat", "reflection", "extra", "Total"]);
    // A group with no entry that day keeps an empty cell.
    expect(rows[1]).toEqual(["2026-11-07", "120", "", "7 900", "8 020"]);
    expect(rows[2]).toEqual(["2026-11-01", "300", "", "", "300"]);
    expect(rows[3]).toEqual(["Total", "420", "", "7 900", "8 320"]);
  });

  it("names each group of the answer in the totals, also one with no usage", () => {
    tables();

    const rows = table("Totals by group");
    expect(rows[0]).toEqual([
      "Group",
      "Calls",
      "Input",
      "Output",
      "Cache read",
      "Cache write",
      "Total",
      "Models",
    ]);
    expect(rows[1]).toEqual(["chat", "4", "420", "0", "0", "0", "420", "m-chat"]);
    expect(rows[2]).toEqual(["reflection", "0", "0", "0", "0", "0", "0", NO_MODELS]);
    expect(rows[3]?.at(-1)).toEqual("m-editor, m-lookup");
    expect(rows[4]).toEqual(["Total", "9", "8 320", "0", "0", "0", "8 320", ""]);
  });

  it("gives a table to each group that has workers, and to no other", () => {
    tables();

    const rows = table("The workers of extra");
    expect(rows[0]?.[0]).toEqual("Worker");
    expect(rows[1]).toEqual(["editor", "1", "1 800", "0", "0", "0", "1 800", "m-editor"]);
    expect(rows[2]?.[0]).toEqual("internet");
    expect(rows[3]).toEqual(["Total", "5", "7 900", "0", "0", "0", "7 900", ""]);
    expect(screen.queryByText("The workers of chat")).toBeNull();
  });

  it("folds out the groups of each day, and leaves out a group with no usage", () => {
    tables();

    const folds = [...document.querySelectorAll("details.day")];
    expect(folds.map((fold) => fold.querySelector("summary")?.textContent)).toEqual([
      "2026-11-07",
      "2026-11-01",
    ]);
    const first = folds[0] as HTMLElement;
    const names = [...first.querySelectorAll("tbody th")].map((cell) => cell.textContent);
    expect(names).toEqual(["chat", "extra"]);
    // The fold-out of a day names no model.
    expect(first.textContent).not.toContain("Models");
  });

  it("keeps the groups of an empty window, and drops the tables of days", () => {
    tables({ ...BODY, rows: [] });

    expect(table("Totals by group")[2]?.[0]).toEqual("reflection");
    expect(screen.queryByText("Tokens by day")).toBeNull();
    expect(screen.queryByText("Each day")).toBeNull();
  });
});

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Usage, { EMPTY_TEXT, NO_CORE_TEXT, type UsageAnswer } from "./Usage";
import type { Session } from "../shell/Shell";
import { NOT_CONFIGURED_TEXT } from "../usage/Meters";
import type { UsageBody } from "../usage/data";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "usage", label: "Usage", path: "/usage" }],
  chat_url: "/chat/alex",
  timezone: "America/New_York",
};

const BODY: UsageBody = {
  timezone: "America/New_York",
  days: 30,
  groups: ["chat"],
  rows: [
    {
      date: "2026-11-07",
      groups: {
        chat: {
          calls: 3,
          input: 100,
          output: 20,
          cache_read: 0,
          cache_write: 0,
          total: 120,
          models: ["m-chat"],
          workers: {},
        },
      },
    },
  ],
  totals: {
    chat: {
      calls: 3,
      input: 100,
      output: 20,
      cache_read: 0,
      cache_write: 0,
      total: 120,
      models: ["m-chat"],
      workers: {},
    },
  },
};

const ANSWER: UsageAnswer = {
  days: 30,
  usage_configured: true,
  usage: BODY,
  limits_configured: true,
  limits: {
    windows: [
      {
        name: "5-hour",
        pct_used: "61%",
        pct_left: "39%",
        fill: 61,
        level: "ok",
        badge: "",
        reset_at: "2026-11-07T22:30:00-05:00",
        reset_text: "resets 10:30 pm today",
        reset: false,
      },
    ],
    as_of: "10:14 pm today",
    reason: null,
  },
  timezone: "America/New_York",
  now: "2026-11-07T22:20:00-05:00",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The session and the usage. The list it gives back holds each address asked. */
function stubRoutes(answer: Response): string[] {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    if (path.startsWith("/api/v1/usage")) {
      asked.push(path);
      return Promise.resolve(answer.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(address = "/usage") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/usage" element={<Usage />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the usage page", () => {
  it("shows the meters, the chart and the tables of the answer", async () => {
    const asked = stubRoutes(json(ANSWER));

    page();

    expect(await screen.findByText("Subscription limits")).toBeDefined();
    expect(asked).toEqual(["/api/v1/usage"]);
    expect(screen.getByText("5-hour").className).toEqual("limit-name");
    expect(document.querySelector("figure.j-card.usage-chart svg")).not.toBeNull();
    const headings = [...document.querySelectorAll("h2")].map((item) => item.textContent);
    expect(headings).toEqual([
      "Subscription limits",
      "Tokens by day",
      "Totals by group",
      "Each day",
    ]);
    expect(screen.getByText("usage").className).toEqual("j-doc-title");
    expect(document.title).toEqual("usage");
    // The content of the usage is the wide one.
    expect(document.querySelector("main.j-content.wide")).not.toBeNull();
  });

  it("reads the window of the address and marks the chip of it", async () => {
    const asked = stubRoutes(json({ ...ANSWER, days: 7 }));

    page("/usage?days=7");

    await screen.findByText("Subscription limits");
    expect(asked).toEqual(["/api/v1/usage?days=7"]);
    const chips = [...document.querySelectorAll("div.j-chips button")];
    expect(chips.map((chip) => chip.textContent)).toEqual([
      "7 days",
      "30 days",
      "90 days",
      "366 days",
    ]);
    expect(chips[0]?.className).toEqual("j-chip on");
    expect(chips[1]?.className).toEqual("j-chip");
  });

  it("says that the usage is not configured, and keeps the meters", async () => {
    stubRoutes(json({ ...ANSWER, usage_configured: false, usage: null }));

    page();

    expect(await screen.findByText("Subscription limits")).toBeDefined();
    expect(screen.getByText("JOSHUA_TOKEN_VIEWER").tagName).toEqual("CODE");
    expect(screen.queryByText("Totals by group")).toBeNull();
  });

  it("says that core did not answer, and that a window is empty", async () => {
    stubRoutes(json({ ...ANSWER, usage: null }));

    page();

    expect(await screen.findByText(NO_CORE_TEXT)).toBeDefined();

    stubRoutes(json({ ...ANSWER, usage: { ...BODY, rows: [] } }));

    page();

    expect(await screen.findByText(EMPTY_TEXT)).toBeDefined();
  });

  it("says that the limits are not configured, and keeps the usage", async () => {
    stubRoutes(json({ ...ANSWER, limits_configured: false, limits: null }));

    page();

    expect(await screen.findByText("Totals by group")).toBeDefined();
    const line = document.querySelector("section.limits p.j-empty");
    expect(line?.textContent).toContain(NOT_CONFIGURED_TEXT);
  });

  it("says what the viewer said when the usage is refused", async () => {
    stubRoutes(json({ error: "forbidden" }, 403));

    page();

    const line = await screen.findByText("forbidden");
    expect(line.className).toEqual("j-error");
  });
});

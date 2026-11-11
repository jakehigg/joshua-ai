import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Meters, {
  NOT_AVAILABLE_TEXT,
  NOT_CONFIGURED_TEXT,
  type LimitWindow,
  type LimitsData,
} from "./Meters";

const FIVE: LimitWindow = {
  name: "5-hour",
  pct_used: "61%",
  pct_left: "39%",
  fill: 61,
  level: "ok",
  badge: "",
  reset_at: "2026-11-07T22:30:00-05:00",
  reset_text: "resets 10:30 pm today",
  reset: false,
};

// A window that reset, as the route sends it: its numbers are old, so the
// route gives it no badge.
const WEEKLY: LimitWindow = {
  name: "Weekly",
  pct_used: "92%",
  pct_left: "8%",
  fill: 92,
  level: "crit",
  badge: "",
  reset_at: "2026-11-09T20:00:00-05:00",
  reset_text: "resets Mon 9 Nov, 8:00 pm",
  reset: true,
};

const LIMITS: LimitsData = {
  windows: [FIVE, WEEKLY],
  as_of: "10:14 pm today",
  reason: null,
};

function meters(limits: LimitsData | null, configured = true) {
  return render(<Meters limits={limits} configured={configured} />);
}

function cards(): string[] {
  return [...document.querySelectorAll("div.limit")].map((card) => card.className);
}

describe("the subscription meters", () => {
  it("shows one meter for each window, with the numbers the route derived", () => {
    meters(LIMITS);

    expect(screen.getByText("Subscription limits").tagName).toEqual("H2");
    expect(screen.getByText("as of 10:14 pm today").className).toEqual("j-meta");
    expect(cards()).toEqual(["j-card limit ok", "j-card limit crit reset"]);
    expect(screen.getByText("5-hour").className).toEqual("limit-name");
    expect(screen.getByText("61%").tagName).toEqual("STRONG");
    expect(screen.getByText("39% left")).toBeDefined();
    const bar = document.querySelector('div.limit-bar[aria-label="5-hour used"]');
    expect(bar?.getAttribute("role")).toEqual("meter");
    expect(bar?.getAttribute("aria-valuenow")).toEqual("61");
    expect(bar?.getAttribute("aria-valuetext")).toEqual("61% used");
    // The bar is inline SVG, and its colour is the class of the card.
    expect(bar?.querySelector("rect.track")).not.toBeNull();
    expect(bar?.querySelector("rect.fill")?.getAttribute("width")).toEqual("61");
    expect(document.querySelector("[style]")).toBeNull();
  });

  it("keeps the last bar of a window that reset, and says what updates it", () => {
    meters(LIMITS);

    const reset = document.querySelector("div.limit.reset");
    expect(reset?.querySelector("rect.fill")?.getAttribute("width")).toEqual("92");
    expect(reset?.textContent).toContain("resets Mon 9 Nov, 8:00 pm");
    // A window that reset carries no badge, because its numbers are old.
    expect(reset?.querySelector(".j-badge")).toBeNull();
  });

  it("marks a window that is high or critical with the badge of the route", () => {
    meters({ ...LIMITS, windows: [{ ...FIVE, level: "warn", badge: "high" }] });

    const badge = screen.getByText("high");
    expect(badge.className).toEqual("j-badge warn");
    expect(cards()).toEqual(["j-card limit warn"]);

    meters({ ...LIMITS, windows: [{ ...WEEKLY, reset: false, badge: "critical" }] });

    expect(screen.getByText("critical").className).toEqual("j-badge bad");
  });

  it("links the reset time to the time core gave", () => {
    meters(LIMITS);

    const time = document.querySelector("time");
    expect(time?.getAttribute("datetime")).toEqual("2026-11-07T22:30:00-05:00");
    expect(time?.textContent).toEqual("resets 10:30 pm today");
  });

  it("says that the limits are not configured, and names what they need", () => {
    meters(null, false);

    expect(screen.getByText("CORE_URL").tagName).toEqual("CODE");
    expect(document.querySelector("p.j-empty")?.textContent).toContain(NOT_CONFIGURED_TEXT);
    expect(cards()).toEqual([]);
  });

  it("says that the limits are not available when core did not answer", () => {
    meters(null);

    expect(screen.getByText(NOT_AVAILABLE_TEXT).className).toEqual("j-empty");
  });

  it("gives the reason of a core that knows nothing yet", () => {
    meters({ windows: [], as_of: null, reason: "the limits are not known yet" });

    const line = screen.getByText("The limits are not available: the limits are not known yet.");
    expect(line.className).toEqual("j-empty");
    expect(cards()).toEqual([]);
  });
});

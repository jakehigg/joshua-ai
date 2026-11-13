import { describe, expect, it } from "vitest";
import type { ChatMessage } from "./store";
import { formatStamp, needsStamp } from "./stamps";

function at(ts: string): ChatMessage {
  return { id: ts, role: "person", text: "x", ts, state: "ok" };
}

describe("needsStamp", () => {
  it("stamps the first message", () => {
    expect(needsStamp(undefined, at("2026-01-01T10:00:00Z"))).toBe(true);
  });
  it("does not stamp a message a few minutes after the one before", () => {
    expect(needsStamp(at("2026-01-01T10:00:00Z"), at("2026-01-01T10:04:59Z"))).toBe(false);
  });
  it("stamps a message more than the gap after the one before", () => {
    expect(needsStamp(at("2026-01-01T10:00:00Z"), at("2026-01-01T10:05:01Z"))).toBe(true);
  });
  it("stamps the first message of a new day", () => {
    expect(needsStamp(at("2026-01-01T23:59:00Z"), at("2026-01-03T00:00:30Z"))).toBe(true);
  });
  it("does not stamp when a time is unreadable", () => {
    expect(needsStamp(at("nope"), at("2026-01-01T10:00:00Z"))).toBe(false);
  });
});

describe("formatStamp", () => {
  const now = new Date(2026, 0, 10, 15, 0, 0);
  it("is the time alone today", () => {
    const s = formatStamp(new Date(2026, 0, 10, 9, 5).toISOString(), now);
    expect(s).toMatch(/9:05/);
    expect(s).not.toMatch(/Jan|Yesterday/);
  });
  it("says Yesterday for the day before", () => {
    expect(formatStamp(new Date(2026, 0, 9, 21, 30).toISOString(), now)).toMatch(/^Yesterday, /);
  });
  it("names the day for an older message in the same year", () => {
    const s = formatStamp(new Date(2026, 0, 2, 8, 0).toISOString(), now);
    expect(s).toMatch(/Jan/);
    expect(s).not.toMatch(/2026/);
  });
  it("adds the year for another year", () => {
    expect(formatStamp(new Date(2025, 11, 31, 8, 0).toISOString(), now)).toMatch(/2025/);
  });
  it("is empty for an unreadable time", () => {
    expect(formatStamp("nope", now)).toBe("");
  });
});

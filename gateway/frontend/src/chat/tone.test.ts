import { describe, expect, it } from "vitest";
import { TONES, toneOf } from "./tone";

describe("toneOf", () => {
  it("gives one writer the same tone every time", () => {
    expect(toneOf("Mia")).toBe(toneOf("Mia"));
    expect(toneOf("Mia")).not.toBe(toneOf("Alex"));
  });

  it("gives a tone that index.css defines, for any name", () => {
    const names = ["Mia", "Lauren", "Alex", "Sam", "Gwen", "Joshua", "", "Zoë 李"];
    for (const name of names) {
      const tone = toneOf(name);
      expect(tone).toMatch(/^tone-[0-5]$/);
      expect(Number(tone.slice(5))).toBeLessThan(TONES);
    }
  });
});

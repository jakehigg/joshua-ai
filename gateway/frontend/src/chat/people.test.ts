import { describe, expect, it } from "vitest";
import { validPerson } from "./people";

describe("validPerson", () => {
  it.each(["alex", "mia", "a", "a-b", "x9", "a".repeat(32)])("accepts %s", (id) => {
    expect(validPerson(id)).toBe(true);
  });

  it.each(["", "Alex", "1abc", "-a", "a".repeat(33), "a_b", "../x", "a b"])(
    "refuses %j",
    (id) => {
      expect(validPerson(id)).toBe(false);
    },
  );
});

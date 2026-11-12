import { describe, expect, it } from "vitest";
import { safeHref } from "./links";

describe("safeHref", () => {
  it("keeps an http(s) address and a path of the viewer", () => {
    expect(safeHref("https://example.com/a?b=1")).toBe("https://example.com/a?b=1");
    expect(safeHref("http://example.com")).toBe("http://example.com");
    expect(safeHref("/documents/alex/pie")).toBe("/documents/alex/pie");
  });

  it("refuses every other scheme and shape", () => {
    for (const url of [
      "javascript:alert(1)",
      "JavaScript:alert(1)",
      " javascript:alert(1)",
      "data:text/html,<b>x</b>",
      "vbscript:x",
      "//evil.example/x",
      "/\\evil.example",
      "ftp://x.test",
      "https://",
      "pie",
      "",
      null,
      undefined,
    ]) {
      expect(safeHref(url)).toBeNull();
    }
  });
});

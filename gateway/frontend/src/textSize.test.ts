/**
 * The text size of the device: what is read, what is kept, and what reaches
 * the root element. The stylesheets do the rest, so the property on <html> is
 * the whole of what the page sees.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  applyTextScale,
  DEFAULT_SCALE,
  readTextScale,
  setTextScale,
  startTextScale,
  STORAGE_KEY,
  TEXT_SIZE_CHOICES,
} from "./textSize";

// The source of `main.tsx`, to see that the size is on the root element
// before React renders.
import mainSource from "./main.tsx?raw";

const onRoot = () => document.documentElement.style.getPropertyValue("--j-text-scale");

beforeEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  document.documentElement.style.removeProperty("--j-text-scale");
});

describe("the text size of this device", () => {
  it("is the normal one while the storage is empty", () => {
    expect(readTextScale()).toEqual(1);
    expect(DEFAULT_SCALE).toEqual(1);

    startTextScale();

    expect(onRoot()).toEqual("1");
  });

  it("offers four sizes, the normal one among them", () => {
    expect(TEXT_SIZE_CHOICES.map((one) => one.label)).toEqual([
      "Small",
      "Normal",
      "Large",
      "Larger",
    ]);
    expect(TEXT_SIZE_CHOICES.map((one) => one.value)).toEqual([0.9, 1, 1.15, 1.3]);
  });

  it("reads a saved choice back and puts it on the root element", () => {
    window.localStorage.setItem(STORAGE_KEY, "1.3");

    expect(readTextScale()).toEqual(1.3);
    startTextScale();

    expect(onRoot()).toEqual("1.3");
  });

  it("keeps a new choice for this device and shows it at once", () => {
    setTextScale(1.15);

    expect(window.localStorage.getItem(STORAGE_KEY)).toEqual("1.15");
    expect(onRoot()).toEqual("1.15");
    expect(readTextScale()).toEqual(1.15);
  });

  it("falls back to the normal size when the storage holds something else", () => {
    window.localStorage.setItem(STORAGE_KEY, "2.5");

    expect(readTextScale()).toEqual(1);
  });

  it("works on when the storage throws, as Safari does in a private window", () => {
    const denied = () => {
      throw new Error("the storage is not available");
    };
    vi.spyOn(window.localStorage, "getItem").mockImplementation(denied);
    vi.spyOn(window.localStorage, "setItem").mockImplementation(denied);

    expect(readTextScale()).toEqual(1);
    startTextScale();
    expect(onRoot()).toEqual("1");

    // The choice holds for this page, and nothing throws.
    setTextScale(0.9);

    expect(onRoot()).toEqual("0.9");
  });

  it("puts a size on the root element and nowhere else", () => {
    applyTextScale(0.9);

    expect(onRoot()).toEqual("0.9");
    expect(document.body.style.getPropertyValue("--j-text-scale")).toEqual("");
  });

  it("is applied before React renders", () => {
    expect(mainSource.indexOf("startTextScale()")).toBeGreaterThan(-1);
    expect(mainSource.indexOf("startTextScale()")).toBeLessThan(mainSource.indexOf("createRoot("));
  });
});

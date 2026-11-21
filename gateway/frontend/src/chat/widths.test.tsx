import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Divider, STEP_PX } from "./Divider";
import {
  MAX_SHARE,
  MIN_REM,
  PanelWidthsPerson,
  STORAGE_KEY,
  breakpointOf,
  keyOf,
  reloadWidths,
  remPx,
} from "./widths";

// The key one width is kept under on this window: the person, the breakpoint
// of the window, and the panel.
const key = (panel: "items" | "doc", person = "alex") =>
  keyOf(person, breakpointOf(window.innerWidth), panel);

// The handle sizes the element after it, which is the panel. jsdom measures
// nothing, so the panel says how wide it is.
function setup(width = 300, stored: Record<string, number> | null = null, person = "alex") {
  if (stored) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  reloadWidths();
  render(
    <PanelWidthsPerson value={person}>
      <div className="chat-screen">
        <Divider panel="items" label="Resize the items column" />
        <aside data-testid="panel" />
      </div>
    </PanelWidthsPerson>,
  );
  const panel = screen.getByTestId("panel");
  Object.defineProperty(panel, "getBoundingClientRect", {
    configurable: true,
    value: () => ({ width }) as DOMRect,
  });
  return screen.getByRole("separator", { name: "Resize the items column" });
}

const kept = () =>
  JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, number>;

describe("the divider of a panel", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    reloadWidths();
  });

  it("makes the panel wider as the pointer goes to the left, and keeps the width", () => {
    const handle = setup(300);
    fireEvent.pointerDown(handle, { button: 0, clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 460, pointerId: 1 });
    fireEvent.pointerUp(handle, { clientX: 460, pointerId: 1 });
    expect(kept()[key("items")]).toBe(340);
    expect(handle.getAttribute("aria-valuenow")).toBe("340");
  });

  it("holds a drag between 14rem and 60 percent of the window", () => {
    const handle = setup(300);
    fireEvent.pointerDown(handle, { button: 0, clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: -4000, pointerId: 1 });
    expect(kept()[key("items")]).toBe(Math.round(window.innerWidth * MAX_SHARE));
    fireEvent.pointerMove(handle, { clientX: 4000, pointerId: 1 });
    expect(kept()[key("items")]).toBe(Math.round(remPx(MIN_REM)));
  });

  it("moves by one step with the arrow keys", () => {
    const handle = setup(300);
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(kept()[key("items")]).toBe(300 + STEP_PX);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(kept()[key("items")]).toBe(300 - STEP_PX);
    // Another key is the browser's business.
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(kept()[key("items")]).toBe(300 - STEP_PX);
  });

  it("puts the panel back to the width of its token on a double-click", () => {
    const handle = setup(300, { [key("items")]: 420 });
    expect(handle.getAttribute("aria-valuenow")).toBe("420");
    fireEvent.doubleClick(handle);
    expect(kept()[key("items")]).toBeUndefined();
    expect(handle.hasAttribute("aria-valuenow")).toBe(false);
  });

  it("restores the width that was kept, and says it is a separator", () => {
    const handle = setup(300, { [key("items")]: 360 });
    expect(handle.getAttribute("role")).toBe("separator");
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle.getAttribute("aria-valuenow")).toBe("360");
    expect(handle.tabIndex).toBe(0);
  });

  it("keeps a width per person, per panel and per breakpoint", () => {
    // The width of another person, of the other panel, and of another
    // breakpoint, is not the width of this handle.
    const handle = setup(300, {
      [key("items", "mia")]: 360,
      [key("doc")]: 380,
      [keyOf("alex", "phone", "items")]: 400,
    });
    expect(handle.hasAttribute("aria-valuenow")).toBe(false);
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    // The drag writes the key of this person, this panel and this breakpoint,
    // and leaves every other width as it was.
    expect(kept()).toEqual({
      [key("items", "mia")]: 360,
      [key("doc")]: 380,
      [keyOf("alex", "phone", "items")]: 400,
      [key("items")]: 300 + STEP_PX,
    });
  });

  it("names a breakpoint for each width where a panel has a size of its own", () => {
    expect(breakpointOf(430)).toBe("phone");
    expect(breakpointOf(899)).toBe("phone");
    expect(breakpointOf(900)).toBe("wide");
    expect(breakpointOf(1099)).toBe("wide");
    expect(breakpointOf(1100)).toBe("widest");
    expect(breakpointOf(1600)).toBe("widest");
  });
});

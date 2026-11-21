import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Divider, STEP_PX } from "./Divider";
import { LIMITS, STORAGE_KEY, reloadWidths, remPx } from "./widths";

// The handle sizes the element after it, which is the panel. jsdom measures
// nothing, so the panel says how wide it is.
function setup(width = 300, stored: Record<string, number> | null = null) {
  if (stored) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  reloadWidths();
  render(
    <div className="chat-screen">
      <Divider panel="items" label="Resize the items column" />
      <aside data-testid="panel" />
    </div>,
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
    expect(kept().items).toBe(340);
    expect(handle.getAttribute("aria-valuenow")).toBe("340");
  });

  it("holds a drag inside the limits of that panel", () => {
    const handle = setup(300);
    fireEvent.pointerDown(handle, { button: 0, clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: -4000, pointerId: 1 });
    expect(kept().items).toBe(Math.round(remPx(LIMITS.items.max)));
    fireEvent.pointerMove(handle, { clientX: 4000, pointerId: 1 });
    expect(kept().items).toBe(Math.round(remPx(LIMITS.items.min)));
  });

  it("moves by one step with the arrow keys", () => {
    const handle = setup(300);
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(kept().items).toBe(300 + STEP_PX);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(kept().items).toBe(300 - STEP_PX);
    // Another key is the browser's business.
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(kept().items).toBe(300 - STEP_PX);
  });

  it("puts the panel back to the width of its token on a double-click", () => {
    const handle = setup(300, { items: 420 });
    expect(handle.getAttribute("aria-valuenow")).toBe("420");
    fireEvent.doubleClick(handle);
    expect(kept().items).toBeUndefined();
    expect(handle.hasAttribute("aria-valuenow")).toBe(false);
  });

  it("restores the width that was kept, and says it is a separator", () => {
    const handle = setup(300, { items: 360 });
    expect(handle.getAttribute("role")).toBe("separator");
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle.getAttribute("aria-valuenow")).toBe("360");
    expect(handle.tabIndex).toBe(0);
  });
});

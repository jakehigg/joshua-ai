import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getHistory } from "../api/client";
import { LIVE_MS, POLL_HIDDEN_MS, useHistory } from "./useHistory";
import type { View } from "./store";

vi.mock("../api/client", () => ({
  getHistory: vi.fn(async () => ({ messages: [], nextBefore: null })),
}));

const calls = () => vi.mocked(getHistory).mock.calls.length;
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function hide(hidden: boolean) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

function mount(live: boolean, isRunning = () => false) {
  const setView = vi.fn((update: View | ((v: View) => View)) => update);
  return renderHook(() =>
    useHistory("alex", setView as never, isRunning, undefined, "shared/t", false, live),
  );
}

describe("useHistory live", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(getHistory).mockClear();
    hide(false);
  });
  afterEach(() => {
    vi.useRealTimers();
    hide(false);
  });

  it("loads a shared thread again every LIVE_MS while the tab shows", async () => {
    mount(true);
    await advance(0);
    expect(calls()).toBe(1);
    await advance(LIVE_MS);
    expect(calls()).toBe(2);
    await advance(LIVE_MS * 2);
    expect(calls()).toBe(4);
  });

  it("waits POLL_HIDDEN_MS while the tab is hidden", async () => {
    hide(true);
    mount(true);
    await advance(0);
    await advance(LIVE_MS * 3);
    expect(calls()).toBe(1);
    await advance(POLL_HIDDEN_MS);
    expect(calls()).toBe(2);
  });

  it("does not load while a turn of this screen streams", async () => {
    let running = false;
    mount(true, () => running);
    await advance(0);
    running = true;
    await advance(LIVE_MS * 3);
    expect(calls()).toBe(1);
  });

  it("does nothing on its own for a thread that is not live", async () => {
    mount(false);
    await advance(0);
    await advance(LIVE_MS * 5);
    expect(calls()).toBe(1);
  });
});

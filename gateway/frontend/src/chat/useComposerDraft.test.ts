import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { SAVE_AFTER_MS, useComposerDraft } from "./useComposerDraft";

// The server holds one draft per person and per thread, as core does.
function draftFake(initial: Record<string, string> = {}) {
  const kept = { ...initial };
  const key = (person: string, workspace?: string) => `${person}/${workspace ?? ""}`;
  const read = vi.spyOn(api, "readDraft").mockImplementation(async (person, workspace) =>
    kept[key(person, workspace)] ?? "",
  );
  const save = vi.spyOn(api, "saveDraft").mockImplementation(async (person, text, workspace) => {
    kept[key(person, workspace)] = text;
    return text;
  });
  return { kept, read, save };
}

describe("useComposerDraft", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("loads the draft of this thread, and nothing before it is read", async () => {
    draftFake({ "alex/": "half a message" });
    const { result } = renderHook(() => useComposerDraft("alex"));
    expect(result.current.text).toBeNull();
    await waitFor(() => expect(result.current.text).toBe("half a message"));
  });

  it("saves once, a short time after the last keystroke", async () => {
    const server = draftFake();
    const { result } = renderHook(() => useComposerDraft("alex", "solar"));
    await waitFor(() => expect(result.current.text).toBe(""));

    for (const text of ["h", "ha", "half"]) act(() => result.current.save(text));
    expect(server.save).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(SAVE_AFTER_MS);
    });

    expect(server.save).toHaveBeenCalledTimes(1);
    expect(server.save).toHaveBeenCalledWith("alex", "half", "solar");
    expect(server.kept["alex/solar"]).toBe("half");
  });

  it("clears the draft at once when the composer goes empty", async () => {
    const server = draftFake({ "alex/": "half a message" });
    const { result } = renderHook(() => useComposerDraft("alex"));
    await waitFor(() => expect(result.current.text).toBe("half a message"));

    act(() => result.current.save(""));

    expect(server.save).toHaveBeenCalledWith("alex", "", undefined);
    expect(server.kept["alex/"]).toBe("");
  });

  it("sends what the timer still holds when the screen goes away", async () => {
    const server = draftFake();
    const { result, unmount } = renderHook(() => useComposerDraft("alex"));
    await waitFor(() => expect(result.current.text).toBe(""));

    act(() => result.current.save("the last word"));
    unmount();

    expect(server.save).toHaveBeenCalledWith("alex", "the last word", undefined);
  });

  it("gives each person their own draft of a shared thread", async () => {
    draftFake({ "alex/solar": "alex is typing", "sam/solar": "sam is typing" });
    const alex = renderHook(() => useComposerDraft("alex", "solar"));
    const sam = renderHook(() => useComposerDraft("sam", "solar"));

    await waitFor(() => expect(alex.result.current.text).toBe("alex is typing"));
    await waitFor(() => expect(sam.result.current.text).toBe("sam is typing"));
  });
});

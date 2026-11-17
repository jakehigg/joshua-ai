import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "./store";
import { MessageList } from "./MessageList";

function msg(serverId: number, extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `s${serverId}`,
    serverId,
    role: "joshua",
    text: `row ${serverId}`,
    ts: "2026-01-01T10:00:00Z",
    state: "ok",
    ...extra,
  };
}

describe("MessageList", () => {
  it("renders a system row as one collapsed line, with no Markdown until it opens", () => {
    render(
      <MessageList messages={[msg(1, { role: "system", text: "**event** happened" })]} canRetry onRetry={() => undefined} />,
    );
    const line = screen.getByText("**event** happened");
    expect(line.className).toBe("event-title");
    expect(line.closest("article")?.className).toContain("msg-system");
    expect(document.querySelector("strong")).toBeNull();
    expect(document.querySelector(".event-body")).toBeNull();
  });

  it("keeps the scroll position when the page before is added", () => {
    const onLoadEarlier = vi.fn();
    const props = { canRetry: true, onRetry: () => undefined, hasEarlier: true, onLoadEarlier };
    const { rerender } = render(<MessageList messages={[msg(5)]} {...props} />);
    const list = screen.getByRole("log");
    let height = 1000;
    Object.defineProperty(list, "scrollHeight", { configurable: true, get: () => height });
    list.scrollTop = 40;
    fireEvent.click(screen.getByRole("button", { name: "Load earlier" }));
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    rerender(<MessageList messages={[msg(5)]} {...props} loadingEarlier />);
    expect((screen.getByRole("button", { name: "Loading…" }) as HTMLButtonElement).disabled).toBe(true);
    height = 1600;
    rerender(<MessageList messages={[msg(3), msg(4), msg(5)]} {...props} hasEarlier={false} />);
    expect(list.scrollTop).toBe(640);
    expect(screen.queryByRole("button", { name: "Load earlier" })).toBeNull();
  });

  it("names the person once for a run of their messages in a shared workspace", () => {
    const person = { role: "person" as const };
    render(
      <MessageList
        messages={[
          msg(1, { ...person, text: "I'll bring pie", who: "Mia" }),
          msg(2, { ...person, text: "and cider", who: "Mia" }),
          msg(3, { text: "Noted." }),
          msg(4, { ...person, text: "me too", who: "Mia" }),
          msg(5, { ...person, text: "the oven is mine", who: "Alex" }),
        ]}
        canRetry
        onRetry={() => undefined}
      />,
    );
    const names = [...document.querySelectorAll(".msg-who")].map((el) => el.textContent);
    expect(names).toEqual(["Mia", "Mia", "Alex"]);
  });

  it("puts the signed-in person's messages at the right and the others at the left", () => {
    const person = { role: "person" as const };
    render(
      <MessageList
        messages={[
          msg(1, { ...person, text: "I'll bring pie", who: "Mia" }),
          msg(2, { ...person, text: "the oven is mine", who: "Alex" }),
          msg(3, { text: "Noted." }),
          msg(4, { ...person, text: "and cider", who: "Mia" }),
        ]}
        canRetry
        onRetry={() => undefined}
        me="Alex"
        shared
      />,
    );
    const sides = [...document.querySelectorAll("article.msg")].map((el) => el.className);
    expect(sides).toEqual([
      "msg msg-person msg-other",
      "msg msg-person",
      "msg msg-joshua",
      "msg msg-person msg-other",
    ]);
    // Each writer at the left is named; the person's own message is not, and
    // Joshua's answer is marked as Joshua.
    const names = [...document.querySelectorAll(".msg-who")].map((el) => el.textContent);
    expect(names).toEqual(["Mia", "Joshua", "Mia"]);
    // Two people, two colours, and Mia keeps hers through the thread.
    const tones = [...document.querySelectorAll(".bubble")].map((el) => el.className);
    expect(new Set(tones).size).toBe(2);
    expect(tones[0]).toBe(tones[2]);
  });

  it("keeps the layout of a personal thread: no name, no colour, all at the right", () => {
    render(
      <MessageList
        messages={[
          msg(1, { role: "person", text: "How long?" }),
          msg(2, { text: "About eleven years." }),
        ]}
        canRetry
        onRetry={() => undefined}
        me="Alex"
      />,
    );
    const sides = [...document.querySelectorAll("article.msg")].map((el) => el.className);
    expect(sides).toEqual(["msg msg-person", "msg msg-joshua"]);
    expect(document.querySelector(".msg-who")).toBeNull();
    expect(document.querySelector(".bubble")?.className).toBe("bubble");
  });

  it("says the thread is read up to the newest row while that row is on screen", () => {
    const onRead = vi.fn();
    const props = { canRetry: true, onRetry: () => undefined, onRead };
    const { rerender } = render(<MessageList messages={[msg(5)]} {...props} />);
    expect(onRead).toHaveBeenCalledWith(5);
    // A row that arrives while the list is at the bottom moves the mark on.
    rerender(<MessageList messages={[msg(5), msg(6)]} {...props} />);
    expect(onRead).toHaveBeenLastCalledWith(6);
    // The reader has scrolled up: a row that arrives now is not on screen.
    const list = screen.getByRole("log");
    Object.defineProperty(list, "scrollHeight", { configurable: true, get: () => 1000 });
    Object.defineProperty(list, "clientHeight", { configurable: true, get: () => 300 });
    list.scrollTop = 0;
    fireEvent.scroll(list);
    onRead.mockClear();
    rerender(<MessageList messages={[msg(5), msg(6), msg(7)]} {...props} />);
    expect(onRead).not.toHaveBeenCalled();
    // The button at the bottom brings it into view, and the mark follows.
    fireEvent.click(screen.getByRole("button", { name: "Scroll to the newest message" }));
    expect(onRead).toHaveBeenCalledWith(7);
  });

  it("shows the loading state and the load error with Retry", () => {
    const onReload = vi.fn();
    const { rerender } = render(<MessageList messages={[]} canRetry onRetry={() => undefined} status="loading" />);
    expect(screen.getByRole("status").textContent).toBe("Loading…");
    expect(screen.queryByText(/Ask Joshua anything/)).toBeNull();
    rerender(
      <MessageList messages={[]} canRetry onRetry={() => undefined} status="error" error="Joshua is unreachable" onReload={onReload} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});

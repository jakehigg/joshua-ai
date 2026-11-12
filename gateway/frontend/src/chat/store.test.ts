import { describe, expect, it } from "vitest";
import type { HistoryMessage, HistoryPage } from "../api/client";
import {
  EMPTY_VIEW,
  appendMessage,
  fromServer,
  keepLocal,
  prependOlder,
  reconcile,
  updateMessage,
  type ChatMessage,
  type View,
} from "./store";

function row(id: number, extra: Partial<HistoryMessage> = {}): HistoryMessage {
  return { id, role: "person", text: `row ${id}`, status: "ok", at: "2026-01-01T10:00:00Z", ...extra };
}

function page(ids: number[], nextBefore: number | null = null): HistoryPage {
  return { messages: ids.map((id) => row(id)), nextBefore };
}

function local(id: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role: "person", text: `local ${id}`, ts: "2026-01-01T10:00:00Z", state: "ok", ...extra };
}

const ids = (v: View) => v.messages.map((m) => m.serverId ?? m.id);

describe("history store", () => {
  it("maps a server row to a message", () => {
    expect(fromServer(row(7, { role: "joshua", status: "failed", turnId: "t-1", tools: ["calendar"] }))).toEqual({
      id: "s7",
      serverId: 7,
      role: "joshua",
      text: "row 7",
      ts: "2026-01-01T10:00:00Z",
      state: "failed",
      turnId: "t-1",
      tools: ["calendar"],
    });
  });

  it("keeps the spoken flag of a person row", () => {
    expect(fromServer(row(8, { spoken: true })).spoken).toBe(true);
    expect("spoken" in fromServer(row(9))).toBe(false);
  });

  it("replaces the list with the server list", () => {
    const view: View = { messages: [fromServer(row(1)), local("old-done", { role: "joshua" })], nextBefore: null };
    const next = reconcile(view, page([1, 2, 3]));
    expect(ids(next)).toEqual([1, 2, 3]);
    expect(next.nextBefore).toBeNull();
  });

  it("keeps an in-flight turn at the end until its done", () => {
    const person = local("p");
    const reply = local("r", { role: "joshua", state: "pending", text: "", forId: "p" });
    const view: View = { messages: [fromServer(row(1)), person, reply], nextBefore: null };
    expect(ids(reconcile(view, page([1, 2])))).toEqual([1, 2, "p", "r"]);
    // After done the answer is no longer pending: the server rows replace both.
    const done = { ...view, messages: [fromServer(row(1)), person, { ...reply, state: "ok" as const }] };
    expect(ids(reconcile(done, page([1, 2, 3])))).toEqual([1, 2, 3]);
  });

  it("keeps a refused person message that was not retried", () => {
    const messages = [local("a", { refused: "Joshua is unreachable" }), local("b", { refused: "x", retried: true }), local("c")];
    expect(keepLocal(messages).map((m) => m.id)).toEqual(["a"]);
  });

  it("keeps the older pages when the new page joins them", () => {
    const view: View = { messages: [1, 2, 3, 4, 5].map((id) => fromServer(row(id))), nextBefore: 1 };
    const next = reconcile(view, page([4, 5, 6], 4));
    expect(ids(next)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(next.nextBefore).toBe(1);
  });

  it("drops the older pages when the new page does not join them", () => {
    const view: View = { messages: [1, 2].map((id) => fromServer(row(id))), nextBefore: 1 };
    const next = reconcile(view, page([10, 11], 10));
    expect(ids(next)).toEqual([10, 11]);
    expect(next.nextBefore).toBe(10);
  });

  it("starts from an empty view and from an empty page", () => {
    expect(reconcile(EMPTY_VIEW, page([3, 4], 3))).toMatchObject({ nextBefore: 3 });
    expect(reconcile({ messages: [fromServer(row(1))], nextBefore: null }, page([]))).toEqual(EMPTY_VIEW);
  });

  it("prepends the page before and keeps the order", () => {
    const view: View = { messages: [3, 4].map((id) => fromServer(row(id))), nextBefore: 3 };
    const next = prependOlder(view, page([1, 2], null));
    expect(ids(next)).toEqual([1, 2, 3, 4]);
    expect(next.nextBefore).toBeNull();
  });

  it("dedupes by id", () => {
    const view: View = { messages: [2, 3].map((id) => fromServer(row(id))), nextBefore: 2 };
    expect(ids(prependOlder(view, page([1, 2], null)))).toEqual([1, 2, 3]);
    expect(ids(reconcile(EMPTY_VIEW, page([1, 1, 2])))).toEqual([1, 2]);
  });

  it("appends and updates in memory", () => {
    const list = appendMessage([local("1")], local("2"));
    expect(updateMessage(list, "2", (m) => ({ text: `${m.text}!` })).map((m) => m.text)).toEqual([
      "local 1",
      "local 2!",
    ]);
    expect(localStorage.length).toBe(0);
  });
});

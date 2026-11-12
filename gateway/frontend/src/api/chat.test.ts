import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, ConnectionLost, getDocumentMeta, getHistory, postTurn, type TurnFrame } from "./client";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("getHistory", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("maps the answer and sends limit and before", async () => {
    const fetch = vi.fn(async () =>
      json({
        messages: [
          { id: 3, role: "person", text: "hi", status: "ok", at: "2026-01-01T10:00:00Z", turnId: "t-1" },
          { id: 4, role: "joshua", text: "[error] boom", status: "failed", at: "2026-01-01T10:00:02Z", tools: ["a", 1] },
          { id: 5, role: "other", text: 9, at: null },
          { id: 7, role: "person", text: "by voice", status: "ok", at: "", spoken: true },
          { id: 8, role: "joshua", text: "not a person", status: "ok", at: "", spoken: true },
          { id: 9, role: "person", text: "bad flag", status: "ok", at: "", spoken: "yes" },
          { id: "6", role: "person", text: "dropped" },
          null,
        ],
        nextBefore: 3,
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const page = await getHistory("alex", { limit: 50, before: 7 });
    expect(fetch).toHaveBeenCalledWith("/api/v1/chat/alex/history?limit=50&before=7", expect.anything());
    expect(page).toEqual({
      messages: [
        { id: 3, role: "person", text: "hi", status: "ok", at: "2026-01-01T10:00:00Z", turnId: "t-1" },
        { id: 4, role: "joshua", text: "[error] boom", status: "failed", at: "2026-01-01T10:00:02Z", tools: ["a"] },
        { id: 5, role: "system", text: "", status: "ok", at: "" },
        { id: 7, role: "person", text: "by voice", status: "ok", at: "", spoken: true },
        { id: 8, role: "joshua", text: "not a person", status: "ok", at: "" },
        { id: 9, role: "person", text: "bad flag", status: "ok", at: "" },
      ],
      nextBefore: 3,
    });
  });

  it("asks with no query when no option is given, and reads a bad cursor as null", async () => {
    const fetch = vi.fn(async () => json({ messages: "nope", nextBefore: "3" }));
    vi.stubGlobal("fetch", fetch);
    expect(await getHistory("mia")).toEqual({ messages: [], nextBefore: null });
    expect(fetch).toHaveBeenCalledWith("/api/v1/chat/mia/history", expect.anything());
  });

  it("sends the workspace as a query value, and none without one", async () => {
    const fetch = vi.fn(async () => json({ messages: [], nextBefore: null }));
    vi.stubGlobal("fetch", fetch);
    await getHistory("alex", { limit: 5, workspace: "notes" });
    expect(fetch).toHaveBeenCalledWith("/api/v1/chat/alex/history?limit=5&workspace=notes", expect.anything());
    await getHistory("alex", { limit: 5 });
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/chat/alex/history?limit=5", expect.anything());
  });

  it("throws ApiError with the route's message and reason for a non-2xx answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: "Joshua is unreachable", reason: "core_unavailable" }, 503)),
    );
    const err = await getHistory("alex").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).message).toBe("Joshua is unreachable");
    expect((err as ApiError).reason).toBe("core_unavailable");
  });

  it("throws ConnectionLost when fetch fails, and passes an abort through", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(getHistory("alex")).rejects.toBeInstanceOf(ConnectionLost);
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(abort)));
    await expect(getHistory("alex")).rejects.toBe(abort);
  });

  it("throws ConnectionLost for a body that is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("not json", { status: 200 })));
    await expect(getHistory("alex")).rejects.toBeInstanceOf(ConnectionLost);
  });
});

describe("getDocumentMeta", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks the viewer's JSON path for the document and maps the answer", async () => {
    const fetch = vi.fn(async () =>
      json({ title: "Pumpkin soup", kind: "artifact", status: "draft", updated: "2026-02-01T10:00:00Z", body: "no" }),
    );
    vi.stubGlobal("fetch", fetch);
    expect(await getDocumentMeta("/documents/sam/pumpkin-soup")).toEqual({
      title: "Pumpkin soup",
      kind: "artifact",
      status: "draft",
      updated: "2026-02-01T10:00:00Z",
    });
    // The viewer's own route for the document.
    expect(fetch).toHaveBeenCalledWith("/documents/sam/pumpkin-soup.json", expect.anything());
  });

  it("reads a key that is not a string as empty", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ title: 7, updated: null })));
    expect(await getDocumentMeta("/documents/sam/notes")).toEqual({
      title: "",
      kind: "",
      status: "",
      updated: "",
    });
  });

  // A card and a panel show nothing of a failure, so every failure is null.
  it("gives null for a 404, a body that is not JSON, a failed call, and an abort", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "not found" }, 404)));
    expect(await getDocumentMeta("/documents/sam/notes")).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 200 })));
    expect(await getDocumentMeta("/documents/sam/notes")).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await getDocumentMeta("/documents/sam/notes")).toBeNull();
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(abort)));
    expect(await getDocumentMeta("/documents/sam/notes")).toBeNull();
  });
});

describe("postTurn", () => {
  afterEach(() => vi.unstubAllGlobals());

  function frames(): TurnFrame[] {
    return [];
  }

  it("posts to the viewer's turn route and reads the frames", async () => {
    const fetch = vi.fn(
      async () =>
        new Response('event: delta\ndata: {"text":"Hi"}\n\nevent: done\ndata: {"turn_id":"t","text":"Hi","tools":[]}\n\n', {
          status: 200,
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const seen = frames();
    await postTurn("alex", "hello", (f) => seen.push(f));
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/chat/alex/turns",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ text: "hello" }) }),
    );
    expect(seen.map((f) => f.type)).toEqual(["delta", "done"]);
  });

  it("encodes the person in the path", async () => {
    const fetch = vi.fn(async () => json({ error: "no" }, 404));
    vi.stubGlobal("fetch", fetch);
    await postTurn("a/b", "x", () => undefined).catch(() => undefined);
    expect((fetch.mock.calls as unknown[][])[0]?.[0]).toBe("/api/v1/chat/a%2Fb/turns");
  });

  it("throws ApiError for a refusal and ConnectionLost when the call fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "too many", reason: "rate_limited" }, 429)));
    const err = await postTurn("alex", "x", () => undefined).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).reason).toBe("rate_limited");
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(postTurn("alex", "x", () => undefined)).rejects.toBeInstanceOf(ConnectionLost);
  });
});

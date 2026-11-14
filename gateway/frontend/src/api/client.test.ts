import { afterEach, describe, expect, it, vi } from "vitest";

import { API_BASE, ApiError, errorText, getJson, postJson } from "./client";

function answer(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the JSON client", () => {
  it("reads a route under /api/v1 with the credentials of the page", async () => {
    const fetchMock = vi.fn().mockResolvedValue(answer({ person: { id: "alex" } }));
    vi.stubGlobal("fetch", fetchMock);

    const body = await getJson<{ person: { id: string } }>("/me");

    expect(body.person.id).toEqual("alex");
    expect(fetchMock.mock.calls[0]?.[0]).toEqual(`${API_BASE}/me`);
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: "GET",
      credentials: "same-origin",
    });
  });

  it("writes a route with the body as JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(answer({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await postJson<{ ok: boolean }>("/links", { title: "Joshua" });

    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ title: "Joshua" }),
    });
  });

  it("throws the status and the error of the viewer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer({ error: "not found" }, 404)));

    const thrown = await getJson("/nope").catch((exc: unknown) => exc);

    expect(thrown).toBeInstanceOf(ApiError);
    expect((thrown as ApiError).status).toEqual(404);
    expect((thrown as ApiError).message).toEqual("not found");
  });

  it("keeps the detail of a refusal, and errorText shows it", async () => {
    const detail = "Lauren cannot use assignments. Clear these servers and try again.";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(answer({ error: "invalid request", detail }, 422)),
    );

    const thrown = await postJson("/workspaces", {}).catch((exc: unknown) => exc);

    expect((thrown as ApiError).message).toEqual("invalid request");
    expect((thrown as ApiError).detail).toEqual(detail);
    expect(errorText(thrown, "failed")).toEqual(detail);
  });

  it("turns a list of validation errors into one line", async () => {
    const detail = [
      { loc: ["body", "title"], msg: "String should have at least 1 character" },
      { loc: ["body", "notify"], msg: "Input should be 'none' or 'done'" },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(answer({ error: "invalid request", detail }, 422)),
    );

    const thrown = await postJson("/workspaces", {}).catch((exc: unknown) => exc);

    expect(errorText(thrown, "failed")).toEqual(
      "title: String should have at least 1 character; notify: Input should be 'none' or 'done'",
    );
  });

  it("errorText falls back to the message, then to the fallback", () => {
    expect(errorText(new ApiError(404, "not found"), "failed")).toEqual("not found");
    expect(errorText("not an error", "failed")).toEqual("failed");
  });

  it("throws the status alone when the body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("gone", { status: 500 })));

    const thrown = await getJson("/me").catch((exc: unknown) => exc);

    expect((thrown as ApiError).status).toEqual(500);
    expect((thrown as ApiError).message).toContain("500");
  });
});

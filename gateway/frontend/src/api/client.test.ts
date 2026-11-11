import { afterEach, describe, expect, it, vi } from "vitest";

import { API_BASE, ApiError, getJson, postJson } from "./client";

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

  it("throws the status alone when the body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("gone", { status: 500 })));

    const thrown = await getJson("/me").catch((exc: unknown) => exc);

    expect((thrown as ApiError).status).toEqual(500);
    expect((thrown as ApiError).message).toContain("500");
  });
});

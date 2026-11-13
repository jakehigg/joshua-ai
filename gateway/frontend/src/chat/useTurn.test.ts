import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as draft from "./draft";
import { repliesWith, stubXhr } from "./fakeXhr";
import type { ChatMessage } from "./store";
import { useTurn } from "./useTurn";

// The browser store of the unsent draft is not under test here.
vi.mock("./draft", () => ({
  saveDraft: vi.fn(async () => undefined),
  clearDraft: vi.fn(async () => undefined),
  loadDraft: vi.fn(async () => null),
}));

// A stream that sends `chunks`, then ends, or errors with `fail`.
// `hold` keeps it open until the test calls release().
function stream(chunks: string[], opts: { fail?: boolean; hold?: boolean } = {}) {
  const enc = new TextEncoder();
  let release = () => undefined as void;
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      if (opts.hold) await new Promise<void>((r) => (release = r));
      // An error drops the queued chunks; let the reader take them first.
      if (opts.fail) await new Promise((r) => setTimeout(r, 20));
      if (opts.fail) controller.error(new TypeError("network error"));
      else controller.close();
    },
  });
  return { body, release: () => release() };
}

function sse(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function fakeFetch(response: Response | (() => Promise<Response>)) {
  const fn = vi.fn(typeof response === "function" ? response : async () => response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function setup(workspace?: string) {
  const onRefused = vi.fn();
  const onDone = vi.fn();
  const onArtifact = vi.fn();
  const hook = renderHook(() => {
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const turn = useTurn("alex", setMessages, onRefused, onDone, onArtifact, workspace);
    return { messages, turn };
  });
  return { hook, onRefused, onDone, onArtifact };
}

describe("useTurn", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("appends deltas, then replaces the text with done.text", async () => {
    const s = stream([sse("delta", { text: "Hel" }), sse("delta", { text: "lo" })], { hold: true });
    const fetch = fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    expect(hook.result.current.turn.running).toBe(true);
    expect(hook.result.current.messages.map((m) => [m.role, m.state])).toEqual([
      ["person", "ok"],
      ["joshua", "pending"],
    ]);
    await waitFor(() => expect(hook.result.current.messages[1]?.text).toBe("Hello"));
    expect(hook.result.current.messages[1]?.state).toBe("pending");
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/chat/alex/turns",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ text: "hi" }) }),
    );
    s.release();
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    // The stream ended with no done frame.
    expect(hook.result.current.messages[1]?.state).toBe("lost");
  });

  it("sets the text to done.text exactly and clears pending", async () => {
    const s = stream([
      ": keepalive\n\n",
      sse("delta", { text: "a" }),
      sse("done", { turn_id: "t1", text: "the whole answer", tools: [] }),
    ]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onDone } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages[1]).toMatchObject({ text: "the whole answer", state: "ok", turnId: "t1" });
    expect(hook.result.current.messages[1]?.forId).toBe(hook.result.current.messages[0]?.id);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(hook.result.current.turn.isRunning()).toBe(false);
  });

  it("holds back a streamed start of [IGNORE], so it never flashes", async () => {
    const s = stream([sse("delta", { text: "[IGN" }), sse("delta", { text: "ORE]" })], { hold: true });
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("event");
    });
    await waitFor(() => expect(hook.result.current.messages).toHaveLength(2));
    await new Promise((r) => setTimeout(r, 30));
    expect(hook.result.current.messages[1]).toMatchObject({ text: "", state: "pending" });
    s.release();
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
  });

  it("shows no bubble for a reply that is only [IGNORE]", async () => {
    const s = stream([
      sse("delta", { text: "[IGN" }),
      sse("delta", { text: "ORE]" }),
      sse("done", { turn_id: "t1", text: " [IGNORE]\n", tools: [] }),
    ]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onDone } = setup();
    act(() => {
      hook.result.current.turn.send("event");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages.map((m) => m.role)).toEqual(["person"]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("shows a streamed text that only starts like the marker once it goes on", async () => {
    const s = stream([sse("delta", { text: "[IG" }), sse("delta", { text: " is a word" }), sse("done", { turn_id: "t1", text: "[IG is a word", tools: [] })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages[1]?.text).toBe("[IG is a word");
  });

  // The `done` frame's `tools` is a de-duped list of tool names and never
  // says which document was written, so the path comes from the fence.
  it("gives the document path of the answer's artifact fence, once", async () => {
    const answer = "applied\n\n```artifact\ndocuments/sam/pumpkin-soup.md\n```\n";
    const s = stream([sse("delta", { text: "app" }), sse("done", { turn_id: "t1", text: answer, tools: ["write_document"] })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onArtifact } = setup();
    act(() => {
      hook.result.current.turn.send("apply my comments");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onArtifact.mock.calls).toEqual([["documents/sam/pumpkin-soup.md"]]);
  });

  it("gives the real .html path of an html artifact fence", async () => {
    const answer = "here\n\n```artifact\ndocuments/alex/garden-plan-mockup.html\n```\n";
    const s = stream([sse("done", { turn_id: "t1", text: answer, tools: ["write_document"] })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onArtifact } = setup();
    act(() => {
      hook.result.current.turn.send("make the mockup");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onArtifact.mock.calls).toEqual([["documents/alex/garden-plan-mockup.html"]]);
  });

  it("gives no path when the answer has no artifact fence, or not a document path", async () => {
    const s = stream([sse("done", { turn_id: "t1", text: "no document here", tools: ["write_document"] })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onArtifact } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onArtifact).not.toHaveBeenCalled();
    const bad = "```artifact\nnotes.md\n```";
    const s2 = stream([sse("done", { turn_id: "t2", text: bad, tools: [] })]);
    fakeFetch(new Response(s2.body, { status: 200 }));
    act(() => {
      hook.result.current.turn.send("hi again");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onArtifact).not.toHaveBeenCalled();
  });

  it("gives no path when the turn failed, fence or not", async () => {
    const fence = "part\n\n```artifact\ndocuments/sam/notes.md\n```";
    const s = stream([sse("delta", { text: fence }), sse("error", { message: "boom" })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onArtifact } = setup();
    act(() => {
      hook.result.current.turn.send("write my notes");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onArtifact).not.toHaveBeenCalled();
  });

  it("marks the answer failed on an error frame and keeps the partial text", async () => {
    const s = stream([sse("delta", { text: "part" }), sse("error", { message: "the connection to Joshua was lost" })]);
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook, onDone } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(onDone).not.toHaveBeenCalled();
    expect(hook.result.current.messages[1]).toMatchObject({
      text: "part",
      state: "failed",
      error: "the connection to Joshua was lost",
      retryText: "hi",
    });
  });

  it("shows a 429 refusal on the person message and in a toast, with no Joshua message", async () => {
    fakeFetch(
      new Response(JSON.stringify({ error: "too many messages, wait a minute", reason: "rate_limited" }), {
        status: 429,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { hook, onRefused } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages).toHaveLength(1);
    expect(hook.result.current.messages[0]).toMatchObject({ role: "person", refused: "too many messages, wait a minute" });
    expect(onRefused).toHaveBeenCalledWith("too many messages, wait a minute");
  });

  it("keeps the partial text and marks the answer lost when the stream breaks", async () => {
    const s = stream([sse("delta", { text: "partial" })], { fail: true });
    fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages[1]).toMatchObject({
      text: "partial",
      state: "lost",
      error: "the connection was lost",
    });
  });

  it("marks the answer lost when fetch fails before an answer", async () => {
    fakeFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(hook.result.current.messages[1]).toMatchObject({ state: "lost", error: "Joshua could not be reached" });
  });

  it("runs one turn at a time", async () => {
    const s = stream([], { hold: true });
    const fetch = fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    let second = true;
    act(() => {
      hook.result.current.turn.send("one");
      second = hook.result.current.turn.send("two");
    });
    expect(second).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(1);
    s.release();
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
  });

  it("retries a failed answer with the same text and hides the old Retry", async () => {
    fakeFetch(async () => new Response(stream([sse("error", { message: "boom" })]).body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("again");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const failed = hook.result.current.messages[1] as ChatMessage;
    act(() => hook.result.current.turn.retry(failed));
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const list = hook.result.current.messages;
    expect(list).toHaveLength(4);
    expect(list[1]?.retried).toBe(true);
    expect(list[2]).toMatchObject({ role: "person", text: "again" });
  });

  it("retries a refused message by sending it again at the end", async () => {
    fakeFetch(
      async () =>
        new Response(JSON.stringify({ error: "Joshua is unreachable" }), { status: 503 }),
    );
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hello");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const refused = hook.result.current.messages[0] as ChatMessage;
    act(() => hook.result.current.turn.retry(refused));
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const list = hook.result.current.messages;
    expect(list).toHaveLength(1);
    expect(list[0]?.id).not.toBe(refused.id);
    expect(list[0]?.refused).toBe("Joshua is unreachable");
  });

  // Records the init of each call, so the FormData body can be read.
  it("sends a file as multipart, keeps its name on the message, and retries with it", async () => {
    const photo = new File(["pixels"], "photo.png", { type: "image/png" });
    const sent = stubXhr(repliesWith([sse("error", { message: "boom" })]));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("what is on this?", photo);
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const form = sent[0]?.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(sent[0]).toMatchObject({ method: "POST", url: "/api/v1/chat/alex/turns" });
    expect(form.get("text")).toBe("what is on this?");
    expect((form.get("file") as File).name).toBe("photo.png");
    expect((form.get("file") as File).type).toBe("image/png");
    expect(sent[0]?.headers).toEqual({ Accept: "text/event-stream" });
    expect(hook.result.current.messages[0]).toMatchObject({ role: "person", fileName: "photo.png", file: photo });
    // Retry sends the same text with the same file.
    act(() => hook.result.current.turn.retry(hook.result.current.messages[1] as ChatMessage));
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(((sent[1]?.body as FormData).get("file") as File).name).toBe("photo.png");
  });

  it("sends a file with no text at all", async () => {
    const pdf = new File(["%PDF"], "bill.pdf", { type: "application/pdf" });
    const requests = stubXhr(repliesWith([sse("done", { turn_id: "t1", text: "ok", tools: [] })]));
    const { hook } = setup();
    let sent = false;
    act(() => {
      sent = hook.result.current.turn.send("", pdf);
    });
    expect(sent).toBe(true);
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    const form = requests[0]?.body as FormData;
    expect(form.get("text")).toBe("");
    expect((form.get("file") as File).name).toBe("bill.pdf");
    expect(hook.result.current.messages[0]?.fileName).toBe("bill.pdf");
  });

  it("sends nothing with neither text nor file", () => {
    const fetch = fakeFetch(new Response(null, { status: 200 }));
    const { hook } = setup();
    let sent = true;
    act(() => {
      sent = hook.result.current.turn.send("  ");
    });
    expect(sent).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the workspace in the JSON body when there is one", async () => {
    const s = stream([sse("done", { turn_id: "t1", text: "ok", tools: [] })]);
    const fetch = fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup("notes");
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/chat/alex/turns",
      expect.objectContaining({ body: JSON.stringify({ text: "hi", workspace: "notes" }) }),
    );
  });

  it("sends the workspace as a form field with a file", async () => {
    const requests = stubXhr(repliesWith([sse("done", { turn_id: "t1", text: "ok", tools: [] })]));
    const { hook } = setup("notes");
    act(() => {
      hook.result.current.turn.send("look", new File(["x"], "a.png", { type: "image/png" }));
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect((requests[0]?.body as FormData).get("workspace")).toBe("notes");
  });

  it("sends no workspace field without one", async () => {
    const s = stream([sse("done", { turn_id: "t1", text: "ok", tools: [] })]);
    const fetch = fakeFetch(new Response(s.body, { status: 200 }));
    const { hook } = setup();
    act(() => {
      hook.result.current.turn.send("hi");
    });
    await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    expect(fetch).toHaveBeenCalledWith("/api/v1/chat/alex/turns", expect.objectContaining({ body: JSON.stringify({ text: "hi" }) }));
  });

  // --- upload toast, draft, and a stream that breaks after the ack -------------

  describe("a turn with a file", () => {
    const photo = new File(["pixels"], "photo.png", { type: "image/png" });
    const ACCEPTED = sse("accepted", { turn_id: "t-5" });

    function setupWith(onDetached = vi.fn()) {
      const hook = renderHook(() => {
        const [messages, setMessages] = useState<ChatMessage[]>([]);
        const turn = useTurn("alex", setMessages, undefined, undefined, undefined, undefined, onDetached);
        return { messages, turn };
      });
      return { hook, onDetached };
    }

    afterEach(() => vi.clearAllMocks());

    it("shows the upload percent, then saving, then sent, then nothing when the answer starts", async () => {
      const sent = stubXhr();
      const { hook } = setupWith();
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      expect(hook.result.current.turn.upload).toEqual({ phase: "uploading", percent: 0, kind: "photo" });
      const xhr = sent[0];
      act(() => xhr?.uploaded(42, 100));
      expect(hook.result.current.turn.upload).toEqual({ phase: "uploading", percent: 42, kind: "photo" });
      act(() => xhr?.uploaded(100, 100));
      expect(hook.result.current.turn.upload).toMatchObject({ phase: "saving", percent: 100 });
      act(() => xhr?.chunk(ACCEPTED));
      expect(hook.result.current.turn.upload).toMatchObject({ phase: "sent" });
      act(() => xhr?.chunk(sse("delta", { text: "It is" })));
      expect(hook.result.current.turn.upload).toBeNull();
      act(() => {
        xhr?.chunk(sse("done", { turn_id: "t-5", text: "It is a cat", tools: [] }));
        xhr?.answer(200);
      });
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    });

    it("names a PDF a file in the toast", () => {
      stubXhr();
      const { hook } = setupWith();
      act(() => {
        hook.result.current.turn.send("", new File(["%PDF"], "a.pdf", { type: "application/pdf" }));
      });
      expect(hook.result.current.turn.upload).toMatchObject({ kind: "file" });
    });

    it("asks the browser to warn on leaving only while the bytes are going out", () => {
      const sent = stubXhr();
      const { hook } = setupWith();
      const add = vi.spyOn(window, "addEventListener");
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      expect(add.mock.calls.some(([type]) => type === "beforeunload")).toBe(true);
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
      act(() => sent[0]?.uploaded(100, 100));
      const later = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(later);
      expect(later.defaultPrevented).toBe(false);
      add.mockRestore();
    });

    it("saves the draft before the request and clears it when Joshua accepts", async () => {
      const sent = stubXhr();
      const { hook } = setupWith();
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      expect(draft.saveDraft).toHaveBeenCalledWith(
        expect.objectContaining({ person: "alex", workspace: "", text: "look", file: photo }),
      );
      expect(draft.clearDraft).not.toHaveBeenCalled();
      act(() => sent[0]?.chunk(ACCEPTED));
      expect(draft.clearDraft).toHaveBeenCalledWith("alex", "");
      act(() => sent[0]?.answer(200, sse("done", { turn_id: "t-5", text: "ok", tools: [] })));
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    });

    it("keeps the draft when the connection fails before the ack", async () => {
      const sent = stubXhr();
      const { hook, onDetached } = setupWith();
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      act(() => sent[0]?.uploaded(30, 100));
      act(() => sent[0]?.fail());
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
      expect(draft.clearDraft).not.toHaveBeenCalled();
      expect(hook.result.current.turn.upload).toBeNull();
      expect(hook.result.current.messages[1]).toMatchObject({ state: "lost" });
      expect(onDetached).not.toHaveBeenCalled();
    });

    it("hands the turn to the page when the stream breaks after the ack", async () => {
      const sent = stubXhr();
      const { hook, onDetached } = setupWith();
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      act(() => sent[0]?.chunk(ACCEPTED));
      act(() => sent[0]?.fail());
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
      expect(onDetached).toHaveBeenCalledWith("t-5");
      // The history shows the message and the answer, so no local copy stays.
      expect(hook.result.current.messages).toEqual([]);
      expect(draft.clearDraft).toHaveBeenCalled();
    });

    it("clears the draft when the backend refuses the message", async () => {
      const sent = stubXhr();
      const { hook } = setupWith();
      act(() => {
        hook.result.current.turn.send("look", photo);
      });
      act(() => sent[0]?.answer(413, '{"error":"too large"}'));
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
      expect(draft.clearDraft).toHaveBeenCalledWith("alex", "");
      expect(hook.result.current.messages[0]).toMatchObject({ refused: "too large" });
    });
  });

  describe("a text turn", () => {
    afterEach(() => vi.clearAllMocks());

    it("keeps the draft until the accepted frame, and shows no upload toast", async () => {
      const s = stream([sse("accepted", { turn_id: "t-6" })], { hold: true });
      fakeFetch(new Response(s.body, { status: 200 }));
      const { hook } = setup();
      act(() => {
        hook.result.current.turn.send("hi");
      });
      expect(draft.saveDraft).toHaveBeenCalledWith(expect.objectContaining({ text: "hi", file: null }));
      expect(hook.result.current.turn.upload).toBeNull();
      await waitFor(() => expect(draft.clearDraft).toHaveBeenCalledWith("alex", ""));
      s.release();
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    });

    it("shows a queued turn as waiting, then as working", async () => {
      const s = stream([sse("accepted", { turn_id: "t-6" }), sse("status", { state: "queued" })], { hold: true });
      fakeFetch(new Response(s.body, { status: 200 }));
      const { hook } = setup();
      act(() => {
        hook.result.current.turn.send("hi");
      });
      await waitFor(() => expect(hook.result.current.messages[1]?.waiting).toBe(true));
      s.release();
      await waitFor(() => expect(hook.result.current.turn.running).toBe(false));
    });
  });
});

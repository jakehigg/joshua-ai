import { describe, expect, it } from "vitest";
import { SseParser } from "./sse";

function feedAll(chunks: string[]) {
  const parser = new SseParser();
  const frames = chunks.flatMap((c) => parser.feed(c));
  return frames.concat(parser.flush());
}

describe("SseParser", () => {
  it("parses one frame", () => {
    expect(feedAll(['event: delta\ndata: {"text":"hi"}\n\n'])).toEqual([
      { event: "delta", data: '{"text":"hi"}' },
    ]);
  });

  it("joins a frame split across chunks at every position", () => {
    const stream = 'event: delta\ndata: {"text":"a"}\n\nevent: done\ndata: {"text":"a"}\n\n';
    for (let i = 1; i < stream.length; i++) {
      expect(feedAll([stream.slice(0, i), stream.slice(i)])).toEqual([
        { event: "delta", data: '{"text":"a"}' },
        { event: "done", data: '{"text":"a"}' },
      ]);
    }
  });

  it("handles CRLF, also split between the CR and the LF", () => {
    expect(feedAll(["event: delta\r", "\ndata: x\r\n\r", "\n"])).toEqual([
      { event: "delta", data: "x" },
    ]);
    expect(feedAll(["event: a\rdata: y\r\r"])).toEqual([{ event: "a", data: "y" }]);
  });

  it("ignores comment lines", () => {
    expect(feedAll([": keepalive\n\n", ": keepalive\n\nevent: done\n: inside\ndata: z\n\n"])).toEqual([
      { event: "done", data: "z" },
    ]);
  });

  it("joins multi-line data with a newline", () => {
    expect(feedAll(["data: one\ndata: two\ndata:three\n\n"])).toEqual([
      { event: "message", data: "one\ntwo\nthree" },
    ]);
  });

  it("passes an unknown event name through", () => {
    expect(feedAll(["event: tool_started\ndata: {}\n\n"])).toEqual([
      { event: "tool_started", data: "{}" },
    ]);
  });

  it("drops a frame with no blank line at the end, and an event with no data", () => {
    expect(feedAll(["event: x\n\n", "event: delta\ndata: cut"])).toEqual([]);
  });
});

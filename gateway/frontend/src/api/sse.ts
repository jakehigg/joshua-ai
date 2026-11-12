// An incremental parser for a server-sent event stream.
//
// feed() takes a decoded text chunk and returns the frames that the chunk
// completes. A frame can be split across chunks at any character. Lines end
// with "\n", "\r\n", or "\r". Comment lines (": keepalive") are ignored.
// The fields "id" and "retry" are ignored; this app does not reconnect.

export interface SseFrame {
  event: string;
  data: string;
}

const LINE_END = /\r\n|\r|\n/;

export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];

  feed(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    for (;;) {
      const match = LINE_END.exec(this.buffer);
      if (!match) break;
      // A "\r" at the end of the buffer can be the first half of "\r\n".
      if (match[0] === "\r" && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const frame = this.line(line);
      if (frame) frames.push(frame);
    }
    return frames;
  }

  // Ends the stream. A frame with no blank line after it is dropped, as the
  // event-stream format says.
  flush(): SseFrame[] {
    const frames = this.buffer.endsWith("\r") ? this.feed("\n") : [];
    this.buffer = "";
    this.event = "";
    this.data = [];
    return frames;
  }

  private line(line: string): SseFrame | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.event = "";
        return null;
      }
      const frame = { event: this.event || "message", data: this.data.join("\n") };
      this.event = "";
      this.data = [];
      return frame;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}

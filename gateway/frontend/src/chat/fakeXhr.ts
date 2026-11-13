// A stand-in for XMLHttpRequest in the tests of a turn with a file. A test
// drives it: uploaded() is upload progress, chunk() is text of the answer that
// has arrived, answer() ends the request, fail() is a network error.

import { vi } from "vitest";

type Progress = { lengthComputable: boolean; loaded: number; total: number };

export class FakeXhr {
  static requests: FakeXhr[] = [];
  static script: ((xhr: FakeXhr) => void) | null = null;

  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: unknown;
  status = 0;
  responseText = "";
  aborted = false;
  upload: { onprogress: ((e: Progress) => void) | null } = { onprogress: null };
  onprogress: (() => void) | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: unknown) {
    this.body = body;
    FakeXhr.requests.push(this);
    FakeXhr.script?.(this);
  }
  abort() {
    this.aborted = true;
  }

  uploaded(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total });
  }
  chunk(text: string) {
    this.status = 200;
    this.responseText += text;
    this.onprogress?.();
  }
  answer(status: number, text = "") {
    this.status = status;
    this.responseText += text;
    this.onload?.();
  }
  fail() {
    this.onerror?.();
  }
}

// Installs the stand-in. `script` runs when a request is sent.
export function stubXhr(script: ((xhr: FakeXhr) => void) | null = null): FakeXhr[] {
  FakeXhr.requests = [];
  FakeXhr.script = script;
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return FakeXhr.requests;
}

// A script that uploads the file at once and answers with `frames`, then ends.
export function repliesWith(frames: string[]): (xhr: FakeXhr) => void {
  return (xhr) => {
    setTimeout(() => {
      xhr.uploaded(1, 1);
      for (const frame of frames) xhr.chunk(frame);
      xhr.answer(200);
    }, 0);
  };
}

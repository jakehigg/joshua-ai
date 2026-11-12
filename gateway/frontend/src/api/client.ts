/**
 * The JSON API of the viewer: every route is under `/api/v1/`.
 *
 * A call carries the cookies and the basic credentials of the page
 * (`same-origin`), because the viewer signs a person in with HTTP basic. A
 * status that is not 2xx throws an `ApiError`, which carries the status and
 * the `error` string of the body, so a page shows what the viewer said.
 */

import { SseParser } from "./sse";

/** The base of every JSON route of the viewer. */
export const API_BASE = "/api/v1";

/** A person of the instance, as every JSON route of the viewer sends them. */
export interface Person {
  id: string;
  name: string;
  role: string;
}

export class ApiError extends Error {
  readonly status: number;
  /** The `reason` of a refusal that came from channels, when the body has one. */
  readonly reason: string | undefined;

  constructor(status: number, message: string, reason?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
  }
}

// The error of a refused call: the `error` and `reason` of a JSON body, else
// the status alone.
function apiErrorOf(status: number, body: unknown): ApiError {
  let message = `the viewer answered ${status}`;
  let reason: string | undefined;
  if (body && typeof body === "object") {
    const { error, reason: why } = body as { error?: unknown; reason?: unknown };
    if (typeof error === "string") {
      message = error;
    }
    if (typeof why === "string") {
      reason = why;
    }
  }
  return new ApiError(status, message, reason);
}

async function readError(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // The body is not JSON: the status is all there is to say.
  }
  return apiErrorOf(response.status, body);
}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    ...init,
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as T;
}

/** Read one JSON route: `getJson<Session>("/session")`. */
export function getJson<T>(path: string): Promise<T> {
  return send<T>(path, { method: "GET", headers: { Accept: "application/json" } });
}

/** Write one JSON route. */
export function postJson<T>(path: string, body: unknown): Promise<T> {
  return send<T>(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Change one thing with a JSON body. */
export function patchJson<T>(path: string, body: unknown): Promise<T> {
  return send<T>(path, {
    method: "PATCH",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Remove one thing. */
export function deleteJson<T>(path: string): Promise<T> {
  return send<T>(path, { method: "DELETE", headers: { Accept: "application/json" } });
}

// The chat calls: one turn, and the history of a conversation.

// The connection failed before the answer, or broke during the stream.
export class ConnectionLost extends Error {
  constructor(message = "the connection was lost") {
    super(message);
    this.name = "ConnectionLost";
  }
}

// One transcript row, as the backend maps it.
export interface HistoryMessage {
  id: number;
  role: "person" | "joshua" | "system";
  text: string;
  status: "ok" | "failed";
  at: string;
  turnId?: string;
  tools?: string[];
  // A person row that was sent with Brief on, in the days of voice; the
  // route removed the marker.
  spoken?: boolean;
  // The files the person sent with the message. `url` is missing when the
  // viewer cannot serve the file to this person.
  attachments?: FileRef[];
}

// One file of a message, as the backend maps it.
export interface FileRef {
  name: string;
  mime: string;
  size?: number;
  url?: string;
}

// The turn that Joshua has accepted and not finished, as the history route
// says it: waiting for another turn of the conversation, or running.
export interface Pending {
  turnId: string;
  state: "queued" | "running";
  since: string;
  // The message of the person, or "" for a turn that is not from a person.
  text: string;
}

// One page, oldest first. `nextBefore` is the cursor of the page before, or null.
export interface HistoryPage {
  messages: HistoryMessage[];
  nextBefore: number | null;
  pending: Pending | null;
}

export type TurnFrame =
  | { type: "accepted"; turnId: string }
  | { type: "status"; state: "queued" | "running" }
  | { type: "delta"; text: string }
  | { type: "done"; turnId: string; text: string; tools: unknown[] }
  | { type: "error"; message: string }
  | { type: "other"; event: string; data: string };

function base(person: string): string {
  return `${API_BASE}/chat/${encodeURIComponent(person)}`;
}

// The abort error comes from the platform's fetch; test it by name, since
// a test environment can have more than one DOMException class.
function isAbort(err: unknown): boolean {
  return !!err && typeof err === "object" && (err as { name?: unknown }).name === "AbortError";
}

function parseFrame(event: string, data: string): TurnFrame {
  let body: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(data);
    if (parsed && typeof parsed === "object") body = parsed as Record<string, unknown>;
  } catch {
    return { type: "other", event, data };
  }
  const str = (key: string) => (typeof body[key] === "string" ? (body[key] as string) : "");
  switch (event) {
    case "accepted":
      return { type: "accepted", turnId: str("turn_id") };
    case "status":
      return { type: "status", state: body.state === "queued" ? "queued" : "running" };
    case "delta":
      return { type: "delta", text: str("text") };
    case "done":
      return {
        type: "done",
        turnId: str("turn_id"),
        text: str("text"),
        tools: Array.isArray(body.tools) ? body.tools : [],
      };
    case "error":
      return { type: "error", message: str("message") || "Joshua could not finish the answer" };
    default:
      return { type: "other", event, data };
  }
}

// The body of one turn: JSON, or multipart when a file comes with it. The
// browser sets the multipart Content-Type itself, with its boundary.
function turnBody(text: string, file?: File | null, workspace?: string): RequestInit {
  if (!file) {
    return {
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(workspace ? { text, workspace } : { text }),
    };
  }
  const form = new FormData();
  form.append("text", text);
  if (workspace) form.append("workspace", workspace);
  form.append("file", file);
  return { headers: { Accept: "text/event-stream" }, body: form };
}

// What the caller hears while a turn goes out.
export interface TurnHooks {
  // Bytes of the request that have gone out. Only a turn with a file reports.
  onUploadProgress?: (loaded: number, total: number) => void;
}

// Reads the frames of one answer. `final` is true after a `done` or an
// `error` frame.
class FrameReader {
  final = false;
  private readonly parser = new SseParser();
  constructor(private readonly onFrame: (frame: TurnFrame) => void) {}

  feed(text: string): void {
    this.handle(this.parser.feed(text));
  }

  finish(rest = ""): void {
    this.handle(this.parser.feed(rest).concat(this.parser.flush()));
  }

  private handle(frames: { event: string; data: string }[]): void {
    for (const { event, data } of frames) {
      const frame = parseFrame(event, data);
      this.onFrame(frame);
      if (frame.type === "done" || frame.type === "error") this.final = true;
    }
  }
}

// A turn with a file goes out with XMLHttpRequest, because only it reports how
// much of the upload has gone out, and Safari cannot stream a request body
// with fetch. The answer is the same event stream: `onprogress` gives the text
// that has arrived so far.
function postTurnXhr(
  url: string,
  body: FormData,
  reader: FrameReader,
  signal: AbortSignal | undefined,
  hooks: TurnHooks,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let seen = 0;
    const pull = () => {
      const text = xhr.responseText;
      if (text.length > seen) {
        const chunk = text.slice(seen);
        seen = text.length;
        reader.feed(chunk);
      }
    };
    const abort = () => {
      xhr.abort();
      reject(new DOMException("The turn was stopped", "AbortError"));
    };
    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    const settled = () => signal?.removeEventListener("abort", abort);
    xhr.open("POST", url);
    xhr.setRequestHeader("Accept", "text/event-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) hooks.onUploadProgress?.(e.loaded, e.total);
    };
    xhr.onprogress = () => {
      if (xhr.status >= 200 && xhr.status < 300) pull();
    };
    xhr.onerror = xhr.ontimeout = () => {
      settled();
      reject(new ConnectionLost(seen === 0 ? "Joshua could not be reached" : undefined));
    };
    xhr.onload = () => {
      settled();
      if (xhr.status < 200 || xhr.status >= 300) {
        let parsed: unknown;
        try {
          parsed = JSON.parse(xhr.responseText);
        } catch {
          // The body is not JSON: the status is all there is to say.
        }
        reject(apiErrorOf(xhr.status, parsed));
        return;
      }
      pull();
      reader.finish();
      if (reader.final) resolve();
      else reject(new ConnectionLost());
    };
    xhr.send(body);
  });
}

// Sends one turn and calls `onFrame` for each frame of the answer.
// Resolves after a `done` or an `error` frame. Throws ApiError for a non-2xx
// answer and ConnectionLost when the stream fails or ends with no final
// frame. An abort through `signal` throws the AbortError unchanged.
// `file` is one photo or one PDF; it makes the request multipart, and
// `hooks.onUploadProgress` hears how much of it has gone out.
// `workspace` (a slug or an id) goes in the body as a `workspace` field. The
// turn then runs in that workspace; without it, in the main thread.
export async function postTurn(
  person: string,
  text: string,
  onFrame: (frame: TurnFrame) => void,
  signal?: AbortSignal,
  file?: File | null,
  workspace?: string,
  hooks: TurnHooks = {},
): Promise<void> {
  const url = `${base(person)}/turns`;
  const reader = new FrameReader(onFrame);
  const init = turnBody(text, file, workspace);
  if (file) return postTurnXhr(url, init.body as FormData, reader, signal, hooks);

  let response: Response;
  try {
    response = await fetch(url, { method: "POST", ...init, signal });
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ConnectionLost("Joshua could not be reached");
  }
  if (!response.ok) throw await readError(response);
  if (!response.body) throw new ConnectionLost();

  const stream = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { done, value } = await stream.read();
      if (done) break;
      reader.feed(decoder.decode(value, { stream: true }));
      if (reader.final) break;
    }
    if (!reader.final) reader.finish(decoder.decode());
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ConnectionLost();
  } finally {
    if (reader.final) stream.cancel().catch(() => undefined);
  }
  if (!reader.final) throw new ConnectionLost();
}

function fileRefs(raw: unknown): FileRef[] {
  const files: FileRef[] = [];
  for (const item of Array.isArray(raw) ? raw : []) {
    if (!item || typeof item !== "object") continue;
    const f = item as Record<string, unknown>;
    if (typeof f.name !== "string") continue;
    files.push({
      name: f.name,
      mime: typeof f.mime === "string" ? f.mime : "application/octet-stream",
      ...(typeof f.size === "number" ? { size: f.size } : {}),
      ...(typeof f.url === "string" && f.url.startsWith("/") ? { url: f.url } : {}),
    });
  }
  return files;
}

function pendingOf(raw: unknown): Pending | null {
  if (!raw || typeof raw !== "object") return null;
  const p = raw as Record<string, unknown>;
  if (typeof p.turnId !== "string") return null;
  return {
    turnId: p.turnId,
    state: p.state === "queued" ? "queued" : "running",
    since: typeof p.since === "string" ? p.since : "",
    text: typeof p.text === "string" ? p.text : "",
  };
}

function historyMessage(raw: unknown): HistoryMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const m = raw as Record<string, unknown>;
  if (typeof m.id !== "number" || !Number.isInteger(m.id)) return null;
  const role = m.role === "person" || m.role === "joshua" ? m.role : "system";
  const message: HistoryMessage = {
    id: m.id,
    role,
    text: typeof m.text === "string" ? m.text : "",
    status: m.status === "failed" ? "failed" : "ok",
    at: typeof m.at === "string" ? m.at : "",
  };
  if (typeof m.turnId === "string") message.turnId = m.turnId;
  if (Array.isArray(m.tools)) message.tools = m.tools.filter((t): t is string => typeof t === "string");
  if (role === "person" && m.spoken === true) message.spoken = true;
  const files = fileRefs(m.attachments);
  if (files.length > 0) message.attachments = files;
  return message;
}

// Reads one page of the conversation from Joshua's transcript. Throws
// ApiError for a non-2xx answer and ConnectionLost when Joshua's backend
// cannot be reached. An abort through `signal` throws the AbortError unchanged.
export async function getHistory(
  person: string,
  opts: { limit?: number; before?: number; signal?: AbortSignal; workspace?: string } = {},
): Promise<HistoryPage> {
  const query = new URLSearchParams();
  if (opts.limit !== undefined) query.set("limit", String(opts.limit));
  if (opts.before !== undefined) query.set("before", String(opts.before));
  if (opts.workspace) query.set("workspace", opts.workspace);
  const qs = query.toString();
  let response: Response;
  try {
    response = await fetch(`${base(person)}/history${qs ? `?${qs}` : ""}`, {
      headers: { Accept: "application/json" },
      signal: opts.signal,
    });
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ConnectionLost("Joshua could not be reached");
  }
  if (!response.ok) throw await readError(response);
  let body: unknown;
  try {
    body = await response.json();
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ConnectionLost("the history answer was not readable");
  }
  const page = (body ?? {}) as { messages?: unknown; nextBefore?: unknown; pending?: unknown };
  const list = Array.isArray(page.messages) ? page.messages : [];
  return {
    messages: list.map(historyMessage).filter((m): m is HistoryMessage => m !== null),
    nextBefore:
      typeof page.nextBefore === "number" && Number.isInteger(page.nextBefore) ? page.nextBefore : null,
    pending: pendingOf(page.pending),
  };
}

// What the viewer knows about one document, with none of its body. It comes
// from the viewer's own route `GET /documents/<owner>/<slug>.json`, a
// host-absolute path, behind the viewer's auth and the same owner checks as
// the document page. The answer is
// `no-store`, reads no body and runs no git command, so it is cheap to ask
// for again.
export interface DocumentMeta {
  title: string;
  kind: string;
  status: string;
  // When the document last changed. An open panel polls for a change of it.
  updated: string;
}

// Reads the title and the stamp of one document. `url` is the viewer's page
// for it (`Artifact.url`); the JSON is that path with `.json`. Any failure
// is null: a 404 for a document that is not this person's, an answer that is
// not readable, a viewer that cannot be reached, and an abort. The caller
// shows nothing then, so a card never carries an error of its own.
export async function getDocumentMeta(url: string, signal?: AbortSignal): Promise<DocumentMeta | null> {
  try {
    const response = await fetch(`${url}.json`, { headers: { Accept: "application/json" }, signal });
    if (!response.ok) return null;
    const body = (await response.json()) as Record<string, unknown> | null;
    if (!body || typeof body !== "object") return null;
    const str = (key: string) => (typeof body[key] === "string" ? (body[key] as string) : "");
    return { title: str("title"), kind: str("kind"), status: str("status"), updated: str("updated") };
  } catch {
    return null;
  }
}

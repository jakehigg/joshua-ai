/**
 * The JSON API of the viewer: every route is under `/api/v1/`.
 *
 * A call carries the cookies and the basic credentials of the page
 * (`same-origin`), because the viewer signs a person in with HTTP basic. A
 * status that is not 2xx throws an `ApiError`, which carries the status and
 * the `error` string of the body, so a page shows what the viewer said.
 */

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

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readError(response: Response): Promise<ApiError> {
  let message = `the viewer answered ${response.status}`;
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string") {
      message = (body as { error: string }).error;
    }
  } catch {
    // The body is not JSON: the status is all there is to say.
  }
  return new ApiError(response.status, message);
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

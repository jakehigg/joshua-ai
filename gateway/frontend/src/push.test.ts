/**
 * The browser half of web push: what the control reads, what one tap does,
 * and what happens when something refuses.
 *
 * The browser's own objects are fakes here: the worker registration with its
 * push manager, the permission of `Notification`, and the four routes of the
 * viewer. jsdom has none of them.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api/client";
import { NO_WORKER, enable, forget, readStatus, sendTest, supportOf } from "./push";

// The public VAPID key of the instance, base64url, as `GET /push/key` sends it.
const KEY = "BLc4xRzKlKORKWlbdgFaBrrPK3ydWAHo4M0gs0i1oek3lP0FI1n1h7MkLxdbCpJ7Nw";
const OTHER_KEY = "BJxYz8Qb0w2dm1XjVCj3xqBk5u7j5m7uMwIkR0F4S9Bq8aEPtQmQ9cM3Ab7Dq1Vs2w";
const ENDPOINT = "https://push.test/abc";

function bytesOf(key: string): Uint8Array {
  const padded = key.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

interface FakeSubscription {
  endpoint: string;
  options: { applicationServerKey?: Uint8Array };
  gone: boolean;
  unsubscribe(): Promise<boolean>;
  toJSON(): unknown;
}

function subscriptionOf(key: string, endpoint = ENDPOINT): FakeSubscription {
  const subscription: FakeSubscription = {
    endpoint,
    options: { applicationServerKey: bytesOf(key) },
    gone: false,
    unsubscribe: async () => {
      subscription.gone = true;
      return true;
    },
    toJSON: () => ({ endpoint, expirationTime: null, keys: { p256dh: "p256", auth: "auth" } }),
  };
  return subscription;
}

interface Call {
  path: string;
  method: string;
  body: unknown;
}

/**
 * One browser: its permission, the worker it registered, the subscription it
 * holds, and the routes of the viewer. The answer carries what the test reads
 * back: every call that went out, and every `subscribe` the browser made.
 */
function stubBrowser({
  permission = "default",
  answers = permission,
  held = null,
  registered = true,
  key = KEY,
  subscribe = () => json({}, 200),
  test = () => json({ sent: 1, failed: 0, dropped: 0, held: 0, error: null }),
}: {
  permission?: NotificationPermission;
  /** What the prompt answers when the control asks. */
  answers?: NotificationPermission;
  held?: FakeSubscription | null;
  registered?: boolean;
  key?: string;
  subscribe?: () => Response;
  test?: () => Response;
} = {}) {
  const calls: Call[] = [];
  const made: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }[] = [];
  const manager = {
    held,
    getSubscription: async () => manager.held,
    subscribe: async (options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }) => {
      made.push(options);
      manager.held = subscriptionOf(key, ENDPOINT);
      return manager.held;
    },
  };
  const registration = { pushManager: manager };
  Object.defineProperty(navigator, "serviceWorker", {
    value: {
      getRegistration: async () => (registered ? registration : undefined),
      ready: Promise.resolve(registration),
    },
    configurable: true,
  });
  vi.stubGlobal("PushManager", class {});
  vi.stubGlobal("Notification", {
    permission,
    requestPermission: async () => answers,
  });
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body === undefined ? undefined : JSON.parse(String(init.body));
    calls.push({ path, method, body });
    if (path === "/api/v1/push/key") return Promise.resolve(json({ key }));
    if (path === "/api/v1/push/subscribe") return Promise.resolve(subscribe());
    if (path === "/api/v1/push/test") return Promise.resolve(test());
    throw new Error(`the page asked for ${path}`);
  });
  return { calls, made, manager };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function appleSafari(): void {
  Object.defineProperty(navigator, "userAgent", {
    value: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Safari/605.1.15",
    configurable: true,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  // Each one shadows what jsdom has; dropping it gives jsdom's own back.
  for (const name of ["serviceWorker", "userAgent", "standalone"]) {
    Reflect.deleteProperty(navigator, name);
  }
});

describe("the state the control reads", () => {
  it("is off until a device subscribes", async () => {
    stubBrowser();

    expect(await readStatus()).toEqual({
      support: "ready",
      permission: "default",
      subscribed: false,
    });
  });

  it("is on when the permission is given and this browser holds one", async () => {
    stubBrowser({ permission: "granted", held: subscriptionOf(KEY) });

    expect(await readStatus()).toEqual({
      support: "ready",
      permission: "granted",
      subscribed: true,
    });
  });

  it("is denied, which only the operating system can undo", async () => {
    stubBrowser({ permission: "denied", held: subscriptionOf(KEY) });

    expect(await readStatus()).toEqual({
      support: "ready",
      permission: "denied",
      subscribed: false,
    });
  });

  it("asks for the installed app on an iPhone, and nothing elsewhere", async () => {
    // iOS has notifications in the app on the home screen, and in no tab.
    appleSafari();
    expect(supportOf()).toBe("install");

    Object.defineProperty(navigator, "standalone", { value: true, configurable: true });
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("Notification", { permission: "default" });
    Object.defineProperty(navigator, "serviceWorker", { value: {}, configurable: true });
    expect(supportOf()).toBe("ready");
  });
});

describe("turning the notifications of a device on", () => {
  it("asks, subscribes with the key of the instance, and stores it", async () => {
    const { calls, made } = stubBrowser({ answers: "granted" });

    expect(await enable()).toEqual({
      support: "ready",
      permission: "granted",
      subscribed: true,
    });

    // iOS allows no silent push, so every subscription is a visible one.
    expect(made).toEqual([{ userVisibleOnly: true, applicationServerKey: bytesOf(KEY) }]);
    expect(calls).toEqual([
      { path: "/api/v1/push/key", method: "GET", body: undefined },
      {
        path: "/api/v1/push/subscribe",
        method: "POST",
        body: { endpoint: ENDPOINT, expirationTime: null, keys: { p256dh: "p256", auth: "auth" } },
      },
    ]);
  });

  it("subscribes again when the instance has a new key", async () => {
    const stale = subscriptionOf(OTHER_KEY);
    const { calls, made } = stubBrowser({ answers: "granted", held: stale });

    await enable();

    expect(stale.gone).toBe(true);
    expect(made).toHaveLength(1);
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /api/v1/push/key",
      "DELETE /api/v1/push/subscribe",
      "POST /api/v1/push/subscribe",
    ]);
  });

  it("keeps the subscription it already has", async () => {
    const { calls, made } = stubBrowser({
      permission: "granted",
      answers: "granted",
      held: subscriptionOf(KEY),
    });

    await enable();

    expect(made).toEqual([]);
    expect(calls.map((call) => call.path)).toEqual([
      "/api/v1/push/key",
      "/api/v1/push/subscribe",
    ]);
  });

  it("leaves nothing behind when the device refuses", async () => {
    const had = subscriptionOf(KEY);
    const { calls } = stubBrowser({ answers: "denied", held: had });

    expect(await enable()).toEqual({
      support: "ready",
      permission: "denied",
      subscribed: false,
    });

    expect(had.gone).toBe(true);
    expect(calls).toEqual([
      { path: "/api/v1/push/subscribe", method: "DELETE", body: { endpoint: ENDPOINT } },
    ]);
  });

  it("drops the subscription when the viewer will not store it", async () => {
    const { calls, manager } = stubBrowser({
      answers: "granted",
      subscribe: () => json({ error: "notifications are not configured on the Joshua side" }, 503),
    });

    await expect(enable()).rejects.toThrow("notifications are not configured on the Joshua side");

    expect((manager.held as FakeSubscription | null)?.gone).toBe(true);
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      "GET /api/v1/push/key",
      "POST /api/v1/push/subscribe",
      "DELETE /api/v1/push/subscribe",
    ]);
  });

  it("says so when the page has no service worker", async () => {
    stubBrowser({ answers: "granted", registered: false });

    await expect(enable()).rejects.toThrow(NO_WORKER);
  });

  it("says what the viewer said when push is off", async () => {
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("Notification", { permission: "default", requestPermission: async () => "granted" });
    Object.defineProperty(navigator, "serviceWorker", {
      value: {
        getRegistration: async () => ({ pushManager: {} }),
        ready: Promise.resolve({ pushManager: {} }),
      },
      configurable: true,
    });
    vi.stubGlobal("fetch", () => Promise.resolve(json({ error: "push is off" }, 503)));

    const err = await enable().catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).message).toBe("push is off");
  });
});

describe("forgetting a device", () => {
  it("unsubscribes the browser and deletes the subscription, as a sign-out does", async () => {
    const had = subscriptionOf(KEY);
    const { calls } = stubBrowser({ permission: "granted", held: had });

    expect(await forget()).toEqual({
      support: "ready",
      permission: "granted",
      subscribed: false,
    });

    expect(had.gone).toBe(true);
    expect(calls).toEqual([
      { path: "/api/v1/push/subscribe", method: "DELETE", body: { endpoint: ENDPOINT } },
    ]);
  });

  it("asks nothing when this browser holds none", async () => {
    const { calls } = stubBrowser({ permission: "granted" });

    await forget();

    expect(calls).toEqual([]);
  });
});

describe("the test send", () => {
  it("says what each browser of the person did with it", async () => {
    stubBrowser({
      permission: "granted",
      held: subscriptionOf(KEY),
      test: () => json({ sent: 2, failed: 1, dropped: 1, held: 0, error: null }),
    });

    expect(await sendTest()).toEqual({ sent: 2, failed: 1, dropped: 1, held: 0, error: null });
  });
});

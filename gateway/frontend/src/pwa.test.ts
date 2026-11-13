/**
 * The installed app: the manifest, and the rules of the hand-written service
 * worker.
 *
 * The worker is a file of `public/`, so it is not a module of the app. The
 * test reads it and runs it against a small fake of the worker globals, which
 * is enough for the three rules that matter: no answer of `/api/v1/` is ever
 * cached, a slow navigation gets the cached shell, and a hashed asset comes
 * from the cache.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { buildIdOf, stampWorker } from "./buildStamp";
import { holdReload, registerServiceWorker, watchForUpdates } from "./pwa";

// The two files of `public/`, which the build copies to the root as they are.
import manifestSource from "../public/manifest.webmanifest?raw";
import workerSource from "../public/sw.js?raw";

// The names in `public/`, so the test sees whether an icon of the manifest is
// a file of the build.
const PUBLIC_FILES = Object.keys(import.meta.glob("../public/*")).map(
  (path) => path.split("/").pop() ?? "",
);

interface FakeResponse {
  body: string;
  ok: boolean;
  type: string;
  clone(): FakeResponse;
}

function answer(body: string, { ok = true, type = "basic" } = {}): FakeResponse {
  const response: FakeResponse = { body, ok, type, clone: () => response };
  return response;
}

interface FakeRequest {
  url: string;
  method: string;
  mode: string;
}

function request(path: string, { method = "GET", mode = "no-cors" } = {}): FakeRequest {
  return { url: `https://joshua.test${path}`, method, mode };
}

function keyOf(target: FakeRequest | string): string {
  return typeof target === "string" ? target : new URL(target.url).pathname;
}

// The worker under one fake of the worker globals: the listeners it set, the
// one cache it opens, and the calls it made to the network.
function load(fetchStub: (target: FakeRequest | string) => Promise<FakeResponse>) {
  const listeners = new Map<string, (event: unknown) => void>();
  const store = new Map<string, FakeResponse>();
  const deleted: string[] = [];
  const cache = {
    put: async (target: FakeRequest | string, response: FakeResponse) => {
      store.set(keyOf(target), response);
    },
    add: async (url: string) => {
      store.set(keyOf(url), await fetchStub(url));
    },
    match: async (target: FakeRequest | string) => store.get(keyOf(target)),
  };
  const caches = {
    open: async () => cache,
    match: async (target: FakeRequest | string) => cache.match(target),
    keys: async () => ["joshua-test", "joshua-v0"],
    delete: async (name: string) => {
      deleted.push(name);
      return true;
    },
  };
  const self = {
    addEventListener: (name: string, fn: (event: unknown) => void) => listeners.set(name, fn),
    location: { origin: "https://joshua.test" },
    skipWaiting: async () => undefined,
    clients: { claim: async () => undefined },
  };
  const run = new Function("self", "caches", "fetch", stampWorker(workerSource, "test")) as (
    s: unknown,
    c: unknown,
    f: unknown,
  ) => void;
  run(self, caches, fetchStub);

  return {
    store,
    deleted,
    // A fetch event: the answer the worker took over, or undefined when it
    // stood aside and the browser does the request itself.
    fetch: (target: FakeRequest): Promise<FakeResponse> | undefined => {
      let taken: Promise<FakeResponse> | undefined;
      const listener = listeners.get("fetch");
      if (!listener) throw new Error("the worker set no fetch listener");
      listener({
        request: target,
        respondWith: (p: Promise<FakeResponse>) => {
          taken = p;
        },
      });
      return taken;
    },
    lifecycle: async (name: "install" | "activate") => {
      let work: Promise<unknown> = Promise.resolve();
      const listener = listeners.get(name);
      if (!listener) throw new Error(`the worker set no ${name} listener`);
      listener({ waitUntil: (p: Promise<unknown>) => (work = p) });
      await work;
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the manifest of the installed app", () => {
  const manifest = JSON.parse(manifestSource) as Record<string, unknown>;

  it("installs the whole viewer, scoped to the root", () => {
    expect(manifest.name).toBe("Joshua");
    expect(manifest.short_name).toBe("Joshua");
    expect(manifest.start_url).toBe("/");
    expect(manifest.scope).toBe("/");
    expect(manifest.display).toBe("standalone");
    expect(manifest.theme_color).toBe("#080d17");
    expect(manifest.background_color).toBe("#080d17");
  });

  it("declares the icon at 192 and at 512, and no maskable one", () => {
    const icons = manifest.icons as { src: string; sizes: string; purpose: string }[];
    expect(icons.map((icon) => icon.sizes)).toEqual(["192x192", "512x512"]);
    for (const icon of icons) {
      expect(icon.purpose).toBe("any");
      // The file is in `public/`, so the build serves it from the root.
      expect(PUBLIC_FILES).toContain(icon.src.slice(1));
    }
  });
});

describe("the registration", () => {
  it("registers no worker outside a build", () => {
    const register = vi.fn();
    Object.defineProperty(navigator, "serviceWorker", {
      value: { register },
      configurable: true,
    });
    registerServiceWorker();
    window.dispatchEvent(new Event("load"));
    expect(register).not.toHaveBeenCalled();
  });
});

describe("the service worker", () => {
  it("stands aside for every address of the JSON API", () => {
    const worker = load(async () => answer("never"));
    for (const path of [
      "/api/v1/chat/alex/turns",
      "/api/v1/chat/alex/history",
      "/api/v1/workspaces",
      "/api/v1/wiki?path=wiki/Home.md",
    ]) {
      expect(worker.fetch(request(path))).toBeUndefined();
    }
    expect(worker.store.size).toBe(0);
  });

  it("stands aside for a write and for another origin", () => {
    const worker = load(async () => answer("never"));
    expect(worker.fetch(request("/", { method: "POST", mode: "navigate" }))).toBeUndefined();
    const elsewhere = { url: "https://elsewhere.test/x.js", method: "GET", mode: "no-cors" };
    expect(worker.fetch(elsewhere)).toBeUndefined();
  });

  it("caches the shell and the icons when it installs", async () => {
    const worker = load(async (target) => answer(`body of ${keyOf(target)}`));
    await worker.lifecycle("install");
    expect([...worker.store.keys()]).toContain("/");
    expect([...worker.store.keys()]).toContain("/logo-192.png");
    expect([...worker.store.keys()]).toContain("/manifest.webmanifest");
  });

  it("drops the cache of an older version when it activates", async () => {
    const worker = load(async () => answer("x"));
    await worker.lifecycle("activate");
    expect(worker.deleted).toEqual(["joshua-v0"]);
  });

  it("serves a page from the network, and keeps it as the shell", async () => {
    const worker = load(async () => answer("the new shell"));
    const response = await worker.fetch(request("/chat/alex", { mode: "navigate" }))!;
    expect(response.body).toBe("the new shell");
    expect(worker.store.get("/")?.body).toBe("the new shell");
  });

  it("serves the shell from the cache when the network is slow", async () => {
    // The network never answers inside the wait of the worker.
    const worker = load(() => new Promise<FakeResponse>(() => undefined));
    worker.store.set("/", answer("the cached shell"));
    vi.useFakeTimers();
    const pending = worker.fetch(request("/wiki/Home.md", { mode: "navigate" }))!;
    await vi.advanceTimersByTimeAsync(10_000);
    expect((await pending).body).toBe("the cached shell");
  });

  it("serves a hashed asset from the cache and asks the network once", async () => {
    let calls = 0;
    const worker = load(async () => {
      calls += 1;
      return answer("console.log(1)");
    });
    const asset = request("/assets/main-abc123.js");
    expect((await worker.fetch(asset)!).body).toBe("console.log(1)");
    expect((await worker.fetch(asset)!).body).toBe("console.log(1)");
    expect(calls).toBe(1);
  });

  it("keeps no answer that is not a plain 200 of this origin", async () => {
    const worker = load(async () => answer("sign in", { ok: false }));
    await worker.fetch(request("/assets/main-abc123.js"));
    expect(worker.store.size).toBe(0);
  });
});

describe("the build stamp", () => {
  it("replaces the placeholder of the worker", () => {
    expect(workerSource).toContain("joshua-__BUILD_ID__");
    const stamped = stampWorker(workerSource, "abc123");
    expect(stamped).toContain('"joshua-abc123"');
    expect(stamped).not.toContain("__BUILD_ID__");
  });

  it("takes BUILD_ID when set, else a hash of the index page", async () => {
    expect(await buildIdOf("<html>", "sha-1")).toBe("sha-1");
    const a = await buildIdOf("<html>a</html>", undefined);
    expect(a).toMatch(/^[0-9a-f]{12}$/);
    expect(await buildIdOf("<html>b</html>", "")).not.toBe(a);
    expect(await buildIdOf("<html>a</html>", " ")).toBe(a);
  });
});

describe("the update of the page", () => {
  function setup(hadController: boolean) {
    const listeners: Record<string, () => void> = {};
    const update = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "serviceWorker", {
      value: {
        controller: hadController ? {} : null,
        addEventListener: (name: string, fn: () => void) => (listeners[name] = fn),
      },
      configurable: true,
    });
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, reload },
      configurable: true,
    });
    let state: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", { get: () => state, configurable: true });
    const setState = (next: DocumentVisibilityState) => {
      state = next;
      document.dispatchEvent(new Event("visibilitychange"));
    };
    vi.useFakeTimers();
    watchForUpdates({ update } as unknown as ServiceWorkerRegistration);
    return { listeners, update, reload, setState };
  }

  afterEach(() => {
    document.getElementById("update-toast")?.remove();
  });

  it("asks for a new worker on visible, focus, online and every 30 minutes", () => {
    const { update, setState } = setup(true);
    setState("hidden");
    expect(update).not.toHaveBeenCalled();
    setState("visible");
    expect(update).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
    expect(update).toHaveBeenCalledTimes(3);
    vi.advanceTimersByTime(30 * 60 * 1000);
    expect(update).toHaveBeenCalledTimes(4);
  });

  it("does not reload on the first claim of a page", () => {
    const { listeners, reload } = setup(false);
    listeners.controllerchange?.();
    expect(reload).not.toHaveBeenCalled();
    expect(document.getElementById("update-toast")).toBeNull();
  });

  it("reloads at once when the page is hidden, and only once", () => {
    const { listeners, reload, setState } = setup(true);
    setState("hidden");
    listeners.controllerchange?.();
    listeners.controllerchange?.();
    expect(reload).toHaveBeenCalledTimes(1);
    setState("visible");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows a toast when visible, and reloads on its button or on the next visible", () => {
    const { listeners, reload, setState } = setup(true);
    listeners.controllerchange?.();
    const toast = document.getElementById("update-toast");
    expect(toast?.textContent).toContain("Joshua was updated");
    expect(reload).not.toHaveBeenCalled();
    toast?.querySelector("button")?.click();
    expect(reload).toHaveBeenCalledTimes(1);
    // A second path: back from the background.
    setState("hidden");
    setState("visible");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("reloads on the next visible when the page was visible at the change", () => {
    const { listeners, reload, setState } = setup(true);
    listeners.controllerchange?.();
    setState("hidden");
    setState("visible");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("holds the reload while a send is going out", () => {
    const { listeners, reload, setState } = setup(true);
    const release = holdReload();
    setState("hidden");
    listeners.controllerchange?.();
    expect(reload).not.toHaveBeenCalled();
    release();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

/**
 * The installed app: the registration of the service worker, and the way a new
 * build reaches the open page.
 *
 * The worker is `public/sw.js`, served from the root of the viewer, so its
 * scope is `/`, the scope of the manifest. It only runs in a build: in `npm
 * run dev` a cached shell would hide the reload of the dev server.
 *
 * An installed app on a phone resumes from the background with no navigation,
 * so the browser does not look for a new worker by itself. The page asks: when
 * it turns visible, on focus, when the network returns, and every 30 minutes
 * while it is visible. A new worker takes over at once (`skipWaiting` and
 * `clients.claim`), and the `controllerchange` that follows loads the new
 * build: a page that is hidden reloads at once; a visible page shows a toast
 * with a Reload button and reloads the next time it turns visible. A page
 * reloads at most once for a new controller, and never while a file is going
 * out (`holdReload`).
 *
 * A browser that has no service worker, and a registration that the browser
 * refuses (an insecure origin, for one), change nothing: the app is the same
 * page, served from the network.
 */

export const CHECK_MS = 30 * 60 * 1000;

let holds = 0;
let pendingReload = false;

// Hold the reload of a new build while a send is going out. Returns the release.
export function holdReload(): () => void {
  holds += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holds -= 1;
    if (holds === 0 && pendingReload && document.visibilityState === "hidden") reload();
  };
}

function reload(): void {
  pendingReload = false;
  window.location.reload();
}

function showToast(onReload: () => void): void {
  if (document.getElementById("update-toast")) return;
  const toast = document.createElement("div");
  toast.id = "update-toast";
  toast.className = "update-toast";
  toast.setAttribute("role", "status");
  const text = document.createElement("span");
  text.textContent = "Joshua was updated";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Reload";
  button.addEventListener("click", onReload);
  toast.append(text, button);
  document.body.append(toast);
}

export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return;
  if (!("serviceWorker" in navigator)) return;
  const container = navigator.serviceWorker;
  window.addEventListener("load", () => {
    container
      .register("/sw.js", { scope: "/" })
      .then((registration) => watchForUpdates(registration))
      .catch(() => undefined);
  });
}

export function watchForUpdates(registration: ServiceWorkerRegistration): void {
  const container = navigator.serviceWorker;
  const check = () => {
    registration.update().catch(() => undefined);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
  window.addEventListener("focus", check);
  window.addEventListener("online", check);
  window.setInterval(() => {
    if (document.visibilityState === "visible") check();
  }, CHECK_MS);

  // The first claim of a page that had no worker is not an update.
  let hadController = container.controller !== null;
  let handled = false;
  container.addEventListener("controllerchange", () => {
    if (!hadController) {
      hadController = true;
      return;
    }
    if (handled) return;
    handled = true;
    pendingReload = true;
    if (document.visibilityState === "hidden") {
      if (holds === 0) reload();
      return;
    }
    showToast(reload);
  });
  // On return to the page the toast says the build is old; load it then.
  document.addEventListener("visibilitychange", () => {
    if (pendingReload && document.visibilityState === "visible" && holds === 0) reload();
  });
}

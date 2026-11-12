/**
 * The installed app: the registration of the service worker.
 *
 * The worker is `public/sw.js`, served from the root of the viewer, so its
 * scope is `/`, the scope of the manifest. It only runs in a build: in `npm
 * run dev` a cached shell would hide the reload of the dev server.
 *
 * A browser that has no service worker, and a registration that the browser
 * refuses (an insecure origin, for one), change nothing: the app is the same
 * page, served from the network.
 */
export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return;
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => undefined);
  });
}

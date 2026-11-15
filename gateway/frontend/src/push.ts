/**
 * Notifications in the browser: the permission, the subscription, and the
 * test send.
 *
 * The server half is core: it holds the VAPID pair and the subscriptions, and
 * it sends (`docs/notifications.md`). This is the half a browser does, over
 * the four routes of the viewer:
 *
 * - `GET /api/v1/push/key` gives the public VAPID key a browser subscribes
 *   with. It answers 503 while the owner has not turned push on, and the
 *   sentence of that answer is what the control shows.
 * - `POST /api/v1/push/subscribe` stores the subscription of this browser.
 * - `DELETE /api/v1/push/subscribe` drops it again.
 * - `POST /api/v1/push/test` sends this person a notification now.
 *
 * Three rules of the platform shape this:
 *
 * - A subscription needs a user gesture that asks for the permission. There is
 *   no silent enrolment, so `enable` runs from a tap and from nothing else.
 * - "denied" is final for the page: only the operating system can undo it, so
 *   the control says that instead of asking again.
 * - On iOS, only an app installed to the home screen has notifications at all
 *   (iOS 16.4 or later). Safari on a tab has no `PushManager`, so `supportOf`
 *   tells that case apart and the control asks the person to install the app.
 *
 * A subscription this browser keeps and the server does not is a notification
 * nobody gets, so the two are dropped together: a refusal anywhere in `enable`
 * unsubscribes the browser and deletes the row (`forget`), and that is also
 * what a sign-out does, while the session still exists.
 */

import { deleteJson, getJson, postJson } from "./api/client";

/** Whether this browser can hold a subscription at all. */
export type Support =
  // It can: a service worker, a `PushManager`, and the notifications of the
  // platform are all here.
  | "ready"
  // iOS, in a tab. Add the app to the home screen and it becomes "ready".
  | "install"
  // No notifications in this browser, and nothing a person can do about it.
  | "unsupported";

/** What the browser answered the last time it was asked. */
export type Permission = "default" | "granted" | "denied";

/** What the control draws. */
export interface PushStatus {
  support: Support;
  permission: Permission;
  /** True when this browser holds a subscription of the signed-in person. */
  subscribed: boolean;
}

/** What `POST /api/v1/push/test` answers: what each browser did with the send. */
export interface TestReport {
  sent: number;
  failed: number;
  dropped: number;
  held: number;
  error: string | null;
}

/** The page holds no worker, so no push can reach it. It is not a build. */
export const NO_WORKER = "this page has no service worker, so no notification can reach it";

/** This browser holds no notifications of its own. */
export const NO_SUPPORT = "this browser cannot show notifications";

// The scope of the worker of the app, which is the scope of the manifest.
const SCOPE = "/";

// True on iPhone and iPad, where a tab has no notifications and the app
// installed to the home screen has them. iPadOS calls itself a Mac, and the
// touch points tell it from one.
function isApple(): boolean {
  const ua = navigator.userAgent;
  if (/iPad|iPhone|iPod/.test(ua)) return true;
  return /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
}

// True for the app as it was installed to the home screen.
function installed(): boolean {
  if (window.matchMedia?.("(display-mode: standalone)").matches) return true;
  return (navigator as Navigator & { standalone?: boolean }).standalone === true;
}

/** Whether this browser can hold a subscription. */
export function supportOf(): Support {
  const ready =
    "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  if (ready) return "ready";
  // On iOS the one thing that is missing is the installed app.
  return isApple() && !installed() ? "install" : "unsupported";
}

function permissionOf(): Permission {
  if (!("Notification" in window)) return "default";
  const permission = Notification.permission;
  return permission === "granted" || permission === "denied" ? permission : "default";
}

// The worker of the app, once it runs. A page of `npm run dev` registers none,
// so `ready` would never settle: the answer is null and the caller says so.
async function worker(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  const existing = await navigator.serviceWorker.getRegistration(SCOPE);
  if (!existing) return null;
  return navigator.serviceWorker.ready;
}

async function current(): Promise<PushSubscription | null> {
  const registration = await worker();
  if (!registration) return null;
  return registration.pushManager.getSubscription();
}

/** What the control draws: the support, the permission, and the subscription. */
export async function readStatus(): Promise<PushStatus> {
  const support = supportOf();
  const permission = permissionOf();
  if (support !== "ready" || permission !== "granted") {
    return { support, permission, subscribed: false };
  }
  return { support, permission, subscribed: (await current()) !== null };
}

// The base64url of the key, as the bytes `subscribe` takes.
function keyBytes(key: string) {
  const padded = key.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

// Drop one subscription in the browser and on the server. Neither failure
// matters to the person: a row the browser no longer holds is dropped by the
// push service's 410 on the next send.
async function drop(subscription: PushSubscription): Promise<void> {
  const { endpoint } = subscription;
  await subscription.unsubscribe().catch(() => false);
  await deleteJson("/push/subscribe", { endpoint }).catch(() => undefined);
}

/**
 * Forget this browser: it keeps no subscription, and the server keeps no row
 * for it. It is the sign-out path, and the path of a refusal.
 */
export async function forget(): Promise<PushStatus> {
  const subscription = await current();
  if (subscription) await drop(subscription);
  return { support: supportOf(), permission: permissionOf(), subscribed: false };
}

// True when the subscription was made with the key the instance has now. A
// pair that changed leaves a subscription nothing can be pushed to, so it is
// dropped and made again.
function sameKey(subscription: PushSubscription, key: string): boolean {
  const held = subscription.options?.applicationServerKey;
  if (!held) return true;
  const wanted = keyBytes(key);
  const bytes = new Uint8Array(held);
  return bytes.length === wanted.length && bytes.every((byte, i) => byte === wanted[i]);
}

/**
 * Ask for the permission, subscribe, and store the subscription. It runs from
 * a tap: the permission prompt needs the gesture.
 *
 * A refusal of the person answers with the status that says so, and leaves
 * nothing behind. Anything else throws: no worker, a browser that cannot
 * subscribe, and the `ApiError` of a viewer that says push is off, whose
 * sentence the control shows.
 */
export async function enable(): Promise<PushStatus> {
  const support = supportOf();
  if (support !== "ready") throw new Error(NO_SUPPORT);
  const permission = (await Notification.requestPermission()) as Permission;
  if (permission !== "granted") {
    // A refused permission revokes what this browser had.
    await forget();
    return { support, permission, subscribed: false };
  }
  const registration = await worker();
  if (!registration) throw new Error(NO_WORKER);
  const { key } = await getJson<{ key: string }>("/push/key");
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription, key)) {
    await drop(subscription);
    subscription = null;
  }
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      // Every push shows a notification. iOS allows no other kind.
      userVisibleOnly: true,
      applicationServerKey: keyBytes(key),
    });
  }
  try {
    await postJson("/push/subscribe", subscription.toJSON());
  } catch (err) {
    // The server would never reach this subscription, so the browser drops it.
    await drop(subscription);
    throw err;
  }
  return { support, permission, subscribed: true };
}

/** Send the signed-in person a test notification, to every browser they hold. */
export function sendTest(): Promise<TestReport> {
  return postJson<TestReport>("/push/test", {});
}

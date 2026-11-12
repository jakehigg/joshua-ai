// The server-backed history of one chat screen.
//
// On open it loads the newest page. It refreshes (loads the newest page
// again and reconciles) when the tab becomes visible, on window focus, and
// after a turn's `done`. A visibility or focus refresh does not run while a
// turn streams, and runs at most once per FOCUS_REFRESH_MS. A new refresh
// stops the one before it. "Load earlier" adds the page before the oldest
// row. A failed first load shows its error with Retry; a later refresh that
// fails keeps the list and shows nothing. `workspace` goes to the history
// route as a query value: the thread of that workspace, else the main thread.
//
// `polling` is true while Joshua works on a turn that this screen does not
// stream: the history said `pending`, or the stream of a turn broke after
// Joshua had accepted it. The newest page is then loaded again every
// POLL_MS while the tab is visible and every POLL_HIDDEN_MS while it is
// hidden, until `polling` is false: the answer is in the transcript, and the
// refresh brings it. A tab that becomes visible refreshes at once, with no
// wait for the rate limit.

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { getHistory } from "../api/client";
import { prependOlder, reconcile, type View } from "./store";

export const PAGE_SIZE = 50;
export const FOCUS_REFRESH_MS = 10_000;
export const POLL_MS = 3_000;
export const POLL_HIDDEN_MS = 30_000;

export type LoadStatus = "loading" | "ready" | "error";

export interface History {
  status: LoadStatus;
  error: string | null;
  loadingEarlier: boolean;
  // The first load again, after a failure.
  retry: () => void;
  // A refresh after a turn's `done`. It is not rate-limited.
  refresh: () => void;
  loadEarlier: (before: number) => void;
}

function message(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "the history could not be loaded";
}

export function useHistory(
  person: string,
  setView: Dispatch<SetStateAction<View>>,
  isRunning: () => boolean,
  onError?: (message: string) => void,
  workspace?: string,
  polling = false,
): History {
  const [status, setStatus] = useState<LoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const loaded = useRef(false);
  const latest = useRef<AbortController | null>(null);
  const earlier = useRef<AbortController | null>(null);
  const lastAt = useRef(Number.NEGATIVE_INFINITY);
  const isRunningRef = useRef(isRunning);
  isRunningRef.current = isRunning;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const load = useCallback(async () => {
    latest.current?.abort();
    const controller = new AbortController();
    latest.current = controller;
    lastAt.current = Date.now();
    if (!loaded.current) {
      setStatus("loading");
      setError(null);
    }
    try {
      const page = await getHistory(person, { limit: PAGE_SIZE, signal: controller.signal, workspace });
      if (controller.signal.aborted) return;
      setView((view) => reconcile(view, page));
      loaded.current = true;
      setStatus("ready");
      setError(null);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (!loaded.current) {
        setStatus("error");
        setError(message(err));
      }
    } finally {
      if (latest.current === controller) latest.current = null;
    }
  }, [person, workspace, setView]);

  const pollingRef = useRef(polling);
  pollingRef.current = polling;

  const wake = useCallback(() => {
    if (document.visibilityState !== "visible") return;
    if (isRunningRef.current()) return;
    if (!pollingRef.current && Date.now() - lastAt.current < FOCUS_REFRESH_MS) return;
    void load();
  }, [load]);

  useEffect(() => {
    void load();
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    return () => {
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("focus", wake);
      latest.current?.abort();
      earlier.current?.abort();
    };
  }, [load, wake]);

  useEffect(() => {
    if (!polling) return undefined;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      if (!isRunningRef.current()) void load();
      timer = setTimeout(tick, document.visibilityState === "visible" ? POLL_MS : POLL_HIDDEN_MS);
    };
    timer = setTimeout(tick, document.visibilityState === "visible" ? POLL_MS : POLL_HIDDEN_MS);
    return () => clearTimeout(timer);
  }, [polling, load]);

  const refresh = useCallback(() => {
    if (!isRunningRef.current()) void load();
  }, [load]);

  const retry = useCallback(() => void load(), [load]);

  const loadEarlier = useCallback(
    (before: number) => {
      if (earlier.current) return;
      const controller = new AbortController();
      earlier.current = controller;
      setLoadingEarlier(true);
      getHistory(person, { limit: PAGE_SIZE, before, signal: controller.signal, workspace })
        .then((page) => {
          if (!controller.signal.aborted) setView((view) => prependOlder(view, page));
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted) onErrorRef.current?.(message(err));
        })
        .finally(() => {
          if (earlier.current === controller) earlier.current = null;
          if (!controller.signal.aborted) setLoadingEarlier(false);
        });
    },
    [person, workspace, setView],
  );

  return { status, error, loadingEarlier, retry, refresh, loadEarlier };
}

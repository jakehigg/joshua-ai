// The unsent text of the composer, kept on the server.
//
// A person who types half a message and opens another workspace finds the
// text again when they come back, on this device and on any other, because
// the draft lives in core (`/api/v1/chat/<person>/draft`) and not in the
// page. It is the person's own: in a shared workspace, where everybody
// writes in one thread, each composer holds its own draft.
//
// `text` is null while the draft loads, so the composer does not fill the box
// with an empty draft and then with the loaded one. `save` keeps what the
// person typed SAVE_AFTER_MS after they stop typing, not on each keypress. An
// empty text clears the draft at once, as a send does, and a screen that goes
// away saves what is still waiting.
//
// A failed call is silent: the composer works without a draft.
//
// This is not draft.ts, which holds a message that is already on its way to
// Joshua. A draft here waits for the person.

import { useCallback, useEffect, useRef, useState } from "react";

import { readDraft, saveDraft } from "../api/client";

// The composer saves this long after the last keypress.
export const SAVE_AFTER_MS = 700;

export interface ComposerDraft {
  /** The draft of this thread, or null while it loads. */
  text: string | null;
  /** Keeps `text` as the draft of this thread, a short time after the last call. */
  save: (text: string) => void;
}

export function useComposerDraft(person: string, workspace?: string): ComposerDraft {
  const [text, setText] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // What the server holds, and what waits to go there.
  const saved = useRef("");
  const waiting = useRef<string | null>(null);

  const put = useCallback(
    (value: string) => {
      waiting.current = null;
      saved.current = value;
      void saveDraft(person, value, workspace).catch(() => undefined);
    },
    [person, workspace],
  );

  useEffect(() => {
    let live = true;
    void readDraft(person, workspace)
      .then((found) => {
        if (!live) return;
        saved.current = found;
        setText(found);
      })
      .catch(() => {
        if (live) setText("");
      });
    return () => {
      live = false;
    };
  }, [person, workspace]);

  // A screen that goes away, by a reload or by another thread, sends what the
  // timer still holds, so no keystroke of the last moment is lost.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
      if (waiting.current !== null && waiting.current !== saved.current) put(waiting.current);
    },
    [put],
  );

  const save = useCallback(
    (value: string) => {
      if (timer.current) clearTimeout(timer.current);
      waiting.current = value;
      if (value === saved.current) {
        waiting.current = null;
        return;
      }
      // An empty composer clears the draft now: a send must not leave the old
      // text on the server.
      if (value === "") {
        put("");
        return;
      }
      timer.current = setTimeout(() => {
        timer.current = null;
        put(value);
      }, SAVE_AFTER_MS);
    },
    [put],
  );

  return { text, save };
}

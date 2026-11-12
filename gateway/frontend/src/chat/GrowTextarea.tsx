// A textarea that grows with its text, for the free-text fields of the
// workspace dialogs (goal, notes). It starts at `rows` and has no inner
// scroll; the dialog body scrolls instead. The browser's own `field-sizing`
// does this in newer browsers, and the height set here does it everywhere
// else. It also takes the text size and padding of every field (index.css).

import { useLayoutEffect, useRef, type TextareaHTMLAttributes } from "react";

export function GrowTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight > 0) el.style.height = `${el.scrollHeight}px`;
  }, [props.value]);
  return <textarea ref={ref} {...props} className={`grow ${props.className ?? ""}`.trim()} />;
}

// While the keyboard is up, bring the field that has the focus to the middle
// of the dialog body. The short wait lets the keyboard finish opening.
export function scrollFieldIntoView(e: { target: EventTarget }): void {
  const el = e.target as HTMLElement;
  if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  setTimeout(() => el.scrollIntoView?.({ block: "center", behavior: "smooth" }), 250);
}

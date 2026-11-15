/**
 * An HTML document in its sandboxed frame, as tall as its content and as wide
 * as the page.
 *
 * The frame loads `/documents/<owner>/<slug>/artifact`. A document with no
 * scripts has `sandbox="allow-same-origin"` and no `allow-scripts`: nothing in
 * it can run, so the same origin gives the page one thing, a read of the size
 * of the document. The page reads `scrollHeight` and `scrollWidth` of the
 * document when the frame loads, and again when the body of the document
 * changes size (an image arrives) or the page changes width (a phone turns).
 * The frame then has the height of its content, so the only scroll is the
 * scroll of the page. A document with `scripts: true` keeps an opaque origin
 * (`sandbox="allow-scripts"`, never `allow-same-origin`): the page cannot read
 * it, so that frame keeps a fixed height and scrolls inside.
 *
 * A document wider than the page (a 792 px mockup on a 390 px phone) is fitted
 * to the width: the frame keeps the width of the document and is scaled down
 * with `transform: scale`, and the box around it takes the scaled height. The
 * button "Actual size" shows the document at its own size instead, and scrolls
 * sideways inside the frame. The button shows only when the document is
 * wider than the page, and the choice is kept for each document in
 * localStorage. A document that fits is the same in both modes.
 *
 * The button "Full screen" (the full page only; in a frame there is nothing to
 * escape) lifts the frame out of the page: the section is fixed over the whole
 * viewport, above the sidebar and the app bar, so no page chrome and no
 * content column keeps the width any more, and the document gets every pixel
 * the screen has, less the safe-area insets (viewer.css). This is the state
 * that matters on a tablet in the installed app, where a 792 px document had
 * to be scaled down to the 760 px column. The browser's own full-screen API is
 * no help there: an installed app on iPadOS does not offer it. Escape and the
 * same button leave the state again, the page behind does not scroll while it
 * is on (`j-lock`, as the navigation drawer), and the box is measured again at
 * the new width.
 *
 * The note on scripts is not a line of its own: the embed page puts it in its
 * Details fold, and the full page shows it as the ⓘ of the bar.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type FitMode = "fit" | "actual";

const KEY = "joshua-doc-fit:";

/** The scale that fits a document of width `natural` in `avail`: never above 1. */
export function fitScale(avail: number, natural: number): number {
  return avail > 0 && natural > avail ? avail / natural : 1;
}

function readMode(url: string): FitMode {
  try {
    return localStorage.getItem(KEY + url) === "actual" ? "actual" : "fit";
  } catch {
    return "fit";
  }
}

function saveMode(url: string, mode: FitMode): void {
  try {
    localStorage.setItem(KEY + url, mode);
  } catch {
    // No storage: the choice lasts until the page closes.
  }
}

export function scriptsNote(scripts: boolean): string {
  return scripts
    ? "Scripts of this document run in a sandbox, apart from the viewer."
    : "Scripts of this document do not run.";
}

interface Props {
  url: string;
  artifactUrl: string;
  title: string;
  scripts: boolean;
  /** Show the note on scripts as an ⓘ in the bar (the full page). */
  info?: boolean;
  /** Offer "Full screen" (the full page, which is not in a frame itself). */
  fullScreen?: boolean;
}

export function ArtifactFrame({
  url,
  artifactUrl,
  title,
  scripts,
  info = false,
  fullScreen = false,
}: Props) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const innerObserver = useRef<ResizeObserver | null>(null);
  const [mode, setMode] = useState<FitMode>(() => readMode(url));
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const [wide, setWide] = useState(false);
  const [measured, setMeasured] = useState(false);
  const [full, setFull] = useState(false);

  const measure = useCallback(() => {
    const frame = frameRef.current;
    const box = boxRef.current;
    const doc = frame?.contentDocument;
    const root = doc?.documentElement;
    if (!frame || !box || !root) return;
    const avail = box.clientWidth;
    if (avail <= 0) return;
    // The page may not scroll the document up and down: the frame is as tall
    // as the document. A probe at the width of the page tells the natural
    // width, and a probe at no height tells the natural height.
    root.style.overflowY = "hidden";
    frame.style.transform = "none";
    frame.style.width = `${avail}px`;
    const natural = Math.max(root.scrollWidth, avail);
    const isWide = natural > avail + 1;
    const fit = modeRef.current === "fit" && isWide;
    frame.style.width = `${fit ? natural : avail}px`;
    frame.style.height = "0px";
    const height = root.scrollHeight;
    const scale = fit ? fitScale(avail, natural) : 1;
    frame.style.height = `${height}px`;
    frame.style.transform = scale < 1 ? `scale(${scale})` : "none";
    box.style.height = `${Math.ceil(height * scale)}px`;
    setWide(isWide);
    setMeasured(true);
  }, []);

  const watch = useCallback(() => {
    innerObserver.current?.disconnect();
    innerObserver.current = null;
    measure();
    const body = frameRef.current?.contentDocument?.body;
    if (body && typeof ResizeObserver !== "undefined") {
      innerObserver.current = new ResizeObserver(measure);
      innerObserver.current.observe(body);
    }
  }, [measure]);

  // The width of the page changes (a phone turns, a panel opens).
  useEffect(() => {
    const box = boxRef.current;
    if (!box || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [measure]);
  useEffect(() => () => innerObserver.current?.disconnect(), []);
  // The box is as wide as the viewport in the full-screen state and as wide as
  // the content column outside it, so each change is a new measurement. A
  // browser with no ResizeObserver gets it from here too.
  useEffect(measure, [mode, full, measure]);

  // While the frame is full screen, the page behind it does not scroll, and
  // Escape leaves the state.
  useEffect(() => {
    if (!full) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFull(false);
    };
    document.addEventListener("keydown", onKey);
    document.documentElement.classList.add("j-lock");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.documentElement.classList.remove("j-lock");
    };
  }, [full]);

  const toggle = () => {
    const next: FitMode = mode === "fit" ? "actual" : "fit";
    saveMode(url, next);
    setMode(next);
  };

  const showInfo = info;
  return (
    <section className={`d-artifact${full ? " full" : ""}`}>
      {((wide && !scripts) || showInfo || fullScreen) && (
        <div className="d-frame-bar">
          {showInfo && (
            <span className="d-info" role="img" aria-label={scriptsNote(scripts)} title={scriptsNote(scripts)}>
              ⓘ
            </span>
          )}
          {wide && !scripts && (
            <button type="button" className="d-fit" onClick={toggle} aria-pressed={mode === "fit"}>
              {mode === "fit" ? "Actual size" : "Fit width"}
            </button>
          )}
          {fullScreen && (
            <button
              type="button"
              className="d-full"
              onClick={() => setFull(!full)}
              aria-pressed={full}
            >
              {full ? "Leave full screen" : "Full screen"}
            </button>
          )}
        </div>
      )}
      {/* A frame the page may not measure (a document with scripts) has no
          height of its own, so in the full-screen state it takes the height
          that is left (viewer.css). */}
      <div className={`d-frame-box${measured ? "" : " fills"}`} ref={boxRef}>
        <iframe
          ref={frameRef}
          className={`d-frame${measured ? " measured" : ""}`}
          sandbox={scripts ? "allow-scripts" : "allow-same-origin"}
          src={artifactUrl}
          title={title}
          referrerPolicy="no-referrer"
          loading="lazy"
          onLoad={scripts ? undefined : watch}
        />
      </div>
    </section>
  );
}

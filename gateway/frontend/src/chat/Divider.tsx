// The handle on the divider between two panes of the chat.
//
// A drag changes the width of the panel, never the width of the thread, which
// takes the rest of the row. The panel holds between 14rem and 60 percent of
// the window, and the width is kept on the device for that person, that panel
// and that breakpoint (widths.ts); a double-click puts the panel back to the
// width of its token. The handle takes the keyboard too: with the focus on it,
// the left and the right arrow move it by STEP_PX.
//
// It shows only from the width where its panel has a size of its own: the
// document panel, a column of the row or the sheet of a workspace item, from
// 900px, and the items column from 1100px (index.css). Narrower than that a
// panel is a drawer, or a sheet over the whole window, and there is nothing to
// drag.

import { useRef, type KeyboardEvent, type PointerEvent } from "react";

import { usePanelWidths, type Panel } from "./widths";

/** How far an arrow key moves the handle, in px. */
export const STEP_PX = 16;

export function Divider({
  panel,
  label,
  grow = -1,
  factor = 1,
}: {
  panel: Panel;
  /** What the handle is called, for a reader of the screen. */
  label: string;
  /** -1 when the panel stands at the right of the handle, 1 at the left. */
  grow?: -1 | 1;
  /**
   * How much width one pixel of the drag gives. It is 1 for a panel that
   * grows at one edge, and 2 for a sheet that stands in the middle and grows
   * at both, so its edge follows the pointer.
   */
  factor?: number;
}) {
  const widths = usePanelWidths();
  const from = useRef<{ x: number; width: number } | null>(null);

  // The panel is the element after the handle, or the one that holds it when
  // the handle sits on the edge of a sheet.
  const measure = (handle: HTMLElement): number => {
    const target = (handle.nextElementSibling as HTMLElement | null) ?? handle.parentElement;
    return target?.getBoundingClientRect().width ?? 0;
  };

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    from.current = { x: e.clientX, width: measure(e.currentTarget) };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const start = from.current;
    if (!start) return;
    widths.set(panel, start.width + grow * factor * (e.clientX - start.x));
  };

  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!from.current) return;
    from.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const dx = e.key === "ArrowLeft" ? -STEP_PX : STEP_PX;
    widths.set(panel, measure(e.currentTarget) + grow * factor * dx);
  };

  return (
    <div
      className={`panel-divider divider-${panel}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={widths.width[panel]}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => widths.reset(panel)}
      onKeyDown={onKeyDown}
    />
  );
}

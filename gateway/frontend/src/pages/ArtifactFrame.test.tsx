import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactFrame, fitScale } from "./ArtifactFrame";

// The panel is `panelWidth` wide. The document inside says how big it is.
let panelWidth = 390;
const observers: { callback: () => void; target: Element }[] = [];

class FakeObserver {
  constructor(private readonly callback: () => void) {}
  observe(target: Element) {
    observers.push({ callback: this.callback, target });
  }
  disconnect() {
    for (let i = observers.length - 1; i >= 0; i -= 1) {
      if (observers[i]?.callback === this.callback) observers.splice(i, 1);
    }
  }
}

function show(content: { width: number; height: number }, scripts = false, info = false) {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("d-frame-box") ? panelWidth : 0;
  });
  render(
    <ArtifactFrame
      url="/documents/alex/mockup"
      artifactUrl="/documents/alex/mockup/artifact"
      title="Mockup"
      scripts={scripts}
      info={info}
    />,
  );
  const frame = document.querySelector("iframe") as HTMLIFrameElement;
  // jsdom loads no frame, so the test gives it the document it would hold.
  const inner = document.implementation.createHTMLDocument("artifact");
  Object.defineProperty(frame, "contentDocument", { configurable: true, get: () => inner });
  const root = inner.documentElement;
  Object.defineProperty(root, "scrollWidth", { configurable: true, get: () => content.width });
  Object.defineProperty(root, "scrollHeight", { configurable: true, get: () => content.height });
  const box = document.querySelector(".d-frame-box") as HTMLElement;
  return { frame, box, root };
}

describe("the frame of an HTML document", () => {
  beforeEach(() => {
    panelWidth = 390;
    observers.length = 0;
    localStorage.clear();
    vi.stubGlobal("ResizeObserver", FakeObserver);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("computes the scale from the widths, and never above 1", () => {
    expect(fitScale(390, 792)).toBeCloseTo(0.4924, 3);
    expect(fitScale(390, 300)).toBe(1);
    expect(fitScale(0, 792)).toBe(1);
  });

  it("is as tall as its document, with one scroll only", () => {
    const { frame, box, root } = show({ width: 390, height: 1200 });
    fireEvent.load(frame);
    expect(frame.style.height).toBe("1200px");
    expect(box.style.height).toBe("1200px");
    expect(frame.style.transform).toBe("none");
    expect(root.style.overflowY).toBe("hidden");
    expect(frame.className).toContain("measured");
  });

  it("follows the height when the document changes size", () => {
    const { frame, root } = show({ width: 390, height: 500 });
    fireEvent.load(frame);
    expect(frame.style.height).toBe("500px");
    Object.defineProperty(root, "scrollHeight", { configurable: true, get: () => 900 });
    act(() => {
      for (const o of observers.filter((o) => o.target === frame.contentDocument?.body)) o.callback();
    });
    expect(frame.style.height).toBe("900px");
  });

  it("scales a wide document down to the width, and the box takes the scaled height", () => {
    const { frame, box } = show({ width: 792, height: 272 });
    fireEvent.load(frame);
    const scale = 390 / 792;
    expect(frame.style.width).toBe("792px");
    expect(frame.style.transform).toBe(`scale(${scale})`);
    expect(box.style.height).toBe(`${Math.ceil(272 * scale)}px`);
    expect(screen.getByRole("button", { name: "Actual size" })).toBeTruthy();
  });

  it("shows the actual size on the toggle, with the frame at the width of the panel", () => {
    const { frame, box } = show({ width: 792, height: 272 });
    fireEvent.load(frame);
    fireEvent.click(screen.getByRole("button", { name: "Actual size" }));
    expect(frame.style.width).toBe("390px");
    expect(frame.style.transform).toBe("none");
    expect(box.style.height).toBe("272px");
    expect(screen.getByRole("button", { name: "Fit width" })).toBeTruthy();
  });

  it("keeps the choice for the document", () => {
    const first = show({ width: 792, height: 272 });
    fireEvent.load(first.frame);
    fireEvent.click(screen.getByRole("button", { name: "Actual size" }));
    expect(localStorage.getItem("joshua-doc-fit:/documents/alex/mockup")).toBe("actual");
    document.body.innerHTML = "";
    vi.restoreAllMocks();
    const second = show({ width: 792, height: 272 });
    fireEvent.load(second.frame);
    expect(second.frame.style.transform).toBe("none");
    expect(screen.getByRole("button", { name: "Fit width" })).toBeTruthy();
  });

  it("has no toggle when the document fits", () => {
    const { frame } = show({ width: 380, height: 300 });
    fireEvent.load(frame);
    expect(screen.queryByRole("button", { name: /Actual size|Fit width/ })).toBeNull();
  });

  it("measures again when the panel changes width", () => {
    const { frame, box } = show({ width: 792, height: 272 });
    fireEvent.load(frame);
    panelWidth = 792;
    act(() => {
      for (const o of observers.filter((o) => o.target === box)) o.callback();
    });
    expect(frame.style.transform).toBe("none");
    expect(screen.queryByRole("button", { name: "Actual size" })).toBeNull();
  });

  it("is sandboxed with the same origin only when no script can run", () => {
    expect(show({ width: 1, height: 1 }).frame.getAttribute("sandbox")).toBe("allow-same-origin");
    document.body.innerHTML = "";
    vi.restoreAllMocks();
    expect(show({ width: 1, height: 1 }, true).frame.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("does not read a frame of a document with scripts, and offers no toggle", () => {
    const { frame, box } = show({ width: 792, height: 272 }, true);
    fireEvent.load(frame);
    expect(frame.style.height).toBe("");
    expect(box.style.height).toBe("");
    expect(frame.className).not.toContain("measured");
    expect(screen.queryByRole("button", { name: /Actual size|Fit width/ })).toBeNull();
  });

  it("puts the note on scripts in the bar as an ⓘ, when asked, and nowhere as a line", () => {
    show({ width: 1, height: 1 }, false, true);
    expect(screen.getByRole("img", { name: "Scripts of this document do not run." })).toBeTruthy();
    expect(screen.queryByText("Scripts of this document do not run.")).toBeNull();
  });
});

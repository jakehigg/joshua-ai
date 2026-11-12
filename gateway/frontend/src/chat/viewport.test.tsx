import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentPanelProvider, useDocumentPanelState } from "./DocumentPanel";
import { ItemPanel } from "./ItemPanel";
import { Rail } from "./Rail";
import { useKeyboardFit } from "./viewport";

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

// A window of `width` px: a media query "(min-width: N px)" is true from N.
function width(px: number) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query);
    return {
      matches: min ? px >= Number(min[1]) : false,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    };
  }) as unknown as typeof window.matchMedia;
}

function rail(open: boolean, collapsed = false) {
  render(
    <MemoryRouter future={FUTURE}>
      <Rail person="alex" workspaces={[]} open={open} collapsed={collapsed} onNew={vi.fn()} onNavigate={vi.fn()} onClose={vi.fn()} />
    </MemoryRouter>,
  );
  return document.getElementById("chat-rail") as HTMLElement;
}

describe("a closed rail is inert", () => {
  afterEach(() => vi.restoreAllMocks());

  it("is inert on a phone until it opens", () => {
    width(390);
    expect(rail(false).hasAttribute("inert")).toBe(true);
    expect(document.getElementById("chat-rail")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("is not inert on a phone when it is open", () => {
    width(390);
    const el = rail(true);
    expect(el.hasAttribute("inert")).toBe(false);
    expect(el.hasAttribute("aria-hidden")).toBe(false);
  });

  it("is a column on a desktop: not inert, unless it is folded away", () => {
    width(1200);
    expect(rail(false).hasAttribute("inert")).toBe(false);
    document.body.innerHTML = "";
    expect(rail(false, true).hasAttribute("inert")).toBe(true);
  });
});

function items(open: boolean, collapsed = false) {
  function Panel() {
    const state = useDocumentPanelState();
    return (
      <MemoryRouter future={FUTURE}>
        <DocumentPanelProvider panel={state}>
          <ItemPanel slug="solar" refreshKey={0} open={open} collapsed={collapsed} onClose={vi.fn()} />
        </DocumentPanelProvider>
      </MemoryRouter>
    );
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })));
  render(<Panel />);
  return document.getElementById("workspace-items") as HTMLElement;
}

describe("a closed item sheet is inert", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("is inert as a phone sheet until it opens, and its fields with it", () => {
    width(390);
    expect(items(false).hasAttribute("inert")).toBe(true);
  });

  it("is not inert when it is open", () => {
    width(390);
    expect(items(true).hasAttribute("inert")).toBe(false);
  });

  it("is not inert as a wide column, and is inert when that column is folded", () => {
    width(1300);
    expect(items(false).hasAttribute("inert")).toBe(false);
    document.body.innerHTML = "";
    expect(items(false, true).hasAttribute("inert")).toBe(true);
  });

  it("is a sheet, not a column, at 1000px", () => {
    width(1000);
    expect(items(false).hasAttribute("inert")).toBe(true);
  });
});

// A visualViewport that a test moves.
class FakeViewport extends EventTarget {
  height = 800;
  offsetTop = 0;
  move(height: number, offsetTop = 0) {
    this.height = height;
    this.offsetTop = offsetTop;
    this.dispatchEvent(new Event("resize"));
  }
}

function Probe() {
  useKeyboardFit();
  return <textarea aria-label="field" />;
}

describe("useKeyboardFit", () => {
  const root = document.documentElement;
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function install(vv: FakeViewport | undefined) {
    Object.defineProperty(window, "visualViewport", { value: vv, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
  }

  it("sets the variables from the visual viewport, and follows it", () => {
    const vv = new FakeViewport();
    install(vv);
    const { unmount } = render(<Probe />);
    expect(root.classList.contains("vv-fit")).toBe(true);
    expect(root.style.getPropertyValue("--vvh")).toBe("800px");
    expect(root.style.getPropertyValue("--kb-inset")).toBe("0px");
    expect(root.classList.contains("kb-open")).toBe(false);

    act(() => vv.move(480, 0));
    expect(root.style.getPropertyValue("--vvh")).toBe("480px");
    expect(root.style.getPropertyValue("--kb-inset")).toBe("320px");
    expect(root.classList.contains("kb-open")).toBe(true);

    // iOS slid the visible part down: the shell follows the top.
    act(() => vv.move(480, 40));
    expect(root.style.getPropertyValue("--vv-top")).toBe("40px");
    expect(root.style.getPropertyValue("--kb-inset")).toBe("280px");

    unmount();
    expect(root.classList.contains("vv-fit")).toBe(false);
    expect(root.style.getPropertyValue("--vvh")).toBe("");
  });

  it("puts the document back at the top when a field takes the focus", () => {
    vi.useFakeTimers();
    const vv = new FakeViewport();
    install(vv);
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    render(<Probe />);
    vv.height = 450;
    act(() => {
      screen.getByLabelText("field").focus();
    });
    expect(scroll).toHaveBeenCalledWith(0, 0);
    expect(root.style.getPropertyValue("--vvh")).toBe("450px");
    vi.advanceTimersByTime(400);
    expect(scroll.mock.calls.length).toBeGreaterThanOrEqual(3);
    vi.useRealTimers();
  });

  it("does nothing where there is no visualViewport", () => {
    install(undefined);
    render(<Probe />);
    expect(root.classList.contains("vv-fit")).toBe(false);
    expect(root.style.getPropertyValue("--vvh")).toBe("");
  });
});

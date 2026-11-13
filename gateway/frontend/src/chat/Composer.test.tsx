import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer, COUNTER_FROM, MAX_CHARS, MAX_FILE_BYTES, MAX_ROWS } from "./Composer";

function pointer(fine: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(pointer: fine)" ? fine : false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

function setup(busy = false) {
  const onSend = vi.fn().mockReturnValue(true);
  render(<Composer onSend={onSend} busy={busy} />);
  const box = screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement;
  const send = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
  return { onSend, box, send };
}

describe("Composer", () => {
  beforeEach(() => pointer(true));
  afterEach(cleanup);

  it("sends on Enter with a fine pointer, then empties and focuses the box", () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello", null);
    expect(box.value).toBe("");
    expect(document.activeElement).toBe(box);
  });

  it("does not send on Shift+Enter", () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("adds a new line on Enter with a coarse pointer: no send, no prevented default", () => {
    pointer(false);
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    const notPrevented = fireEvent.keyDown(box, { key: "Enter" });
    expect(notPrevented).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
    expect(box.getAttribute("enterkeyhint")).toBe("enter");
  });

  it("sends with the button on a coarse pointer", () => {
    pointer(false);
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("hello", null);
  });

  it("leaves Shift+Enter to the browser on touch: no send, no prevented default", () => {
    pointer(false);
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    const notPrevented = fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(notPrevented).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not send on Enter during an IME composition", () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("is one row when empty, and grows with its text", () => {
    const { box } = setup();
    expect(box.rows).toBe(1);
    expect(box.style.height).toBe("");
    Object.defineProperty(box, "scrollHeight", { value: 96, configurable: true });
    fireEvent.change(box, { target: { value: "a\nb\nc" } });
    expect(box.style.height).toBe("96px");
    fireEvent.change(box, { target: { value: "" } });
    expect(box.style.height).toBe("");
  });

  it("does not send empty or blank text", () => {
    const { onSend, box, send } = setup();
    expect(send.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "  \n " } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(send.disabled).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not send while a turn runs", () => {
    const { onSend, box, send } = setup(true);
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(send.disabled).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("hello");
  });

  it("shows the counter past 7000 characters only", () => {
    const { box } = setup();
    fireEvent.change(box, { target: { value: "a".repeat(COUNTER_FROM) } });
    expect(screen.queryByText(/\/ 8000/)).toBeNull();
    fireEvent.change(box, { target: { value: "a".repeat(COUNTER_FROM + 1) } });
    expect(screen.getByText(/7001 \/ 8000/)).toBeTruthy();
  });

  it("blocks a text over 8000 characters", () => {
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "a".repeat(MAX_CHARS + 1) } });
    expect(send.disabled).toBe(true);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText(/too long to send/)).toBeTruthy();
    fireEvent.change(box, { target: { value: "a".repeat(MAX_CHARS) } });
    expect(send.disabled).toBe(false);
  });
});

describe("Composer attachment", () => {
  beforeEach(() => pointer(true));
  afterEach(cleanup);

  function pick(file: File) {
    const input = screen.getByLabelText("Attach a photo or a PDF") as HTMLInputElement;
    // jsdom has no file picker; set the list the input would have.
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    fireEvent.change(input);
    return input;
  }

  it("takes a photo and sends it with the text, then empties the chip", () => {
    const { onSend, box } = setup();
    const photo = new File(["pixels"], "photo.png", { type: "image/png" });
    pick(photo);
    expect(screen.getByText("photo.png")).toBeTruthy();
    fireEvent.change(box, { target: { value: "what is on this?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("what is on this?", photo);
    expect(screen.queryByText("photo.png")).toBeNull();
  });

  it("sends a PDF with no text at all", () => {
    const { onSend, send } = setup();
    const pdf = new File(["%PDF"], "bill.pdf", { type: "application/pdf" });
    pick(pdf);
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("", pdf);
  });

  it("takes the file off again", () => {
    const { send } = setup();
    pick(new File(["%PDF"], "bill.pdf", { type: "application/pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove the file" }));
    expect(screen.queryByText("bill.pdf")).toBeNull();
    expect(send.disabled).toBe(true);
  });

  it("refuses a file over the limit and says so, with nothing attached", () => {
    const { onSend, box, send } = setup();
    const big = new File(["x"], "huge.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: MAX_FILE_BYTES + 1 });
    pick(big);
    expect(screen.getByRole("alert").textContent).toContain("huge.png is too large");
    expect(screen.queryByText("huge.png")).toBeNull();
    expect(send.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello", null);
  });

  it("only takes images and PDFs", () => {
    setup();
    const input = screen.getByLabelText("Attach a photo or a PDF") as HTMLInputElement;
    expect(input.accept).toBe("image/*,application/pdf");
  });

  it("asks the phone keyboard for a send key, and keeps the file input out of the field order", () => {
    const { box } = setup();
    expect(box.getAttribute("enterkeyhint")).toBe("send");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.tabIndex).toBe(-1);
  });

  it("has the send button disabled when empty, and enabled with text or a file", () => {
    const { box, send } = setup();
    expect(send.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "hi" } });
    expect(send.disabled).toBe(false);
    fireEvent.change(box, { target: { value: "  " } });
    expect(send.disabled).toBe(true);
  });

  it("sits in one pill with the clip and the send button, and starts at one row", () => {
    const { box, send } = setup();
    const pill = document.querySelector(".composer-inner") as HTMLElement;
    expect(pill.contains(box) && pill.contains(send) && pill.querySelector(".attach") !== null).toBe(true);
    expect(box.rows).toBe(1);
    expect(MAX_ROWS).toBe(5);
  });
});

describe("the file preview", () => {
  beforeEach(() => pointer(true));
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  function pick(file: File) {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
  }

  it("shows a photo as a thumbnail with the remove button on its corner", () => {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:p", revokeObjectURL: vi.fn() }));
    render(<Composer onSend={vi.fn()} busy={false} />);
    pick(new File(["x"], "photo.png", { type: "image/png" }));
    const thumb = document.querySelector(".preview-thumb") as HTMLElement;
    expect(thumb.querySelector("img")?.getAttribute("src")).toBe("blob:p");
    const remove = thumb.querySelector("button.chip-remove") as HTMLButtonElement;
    fireEvent.click(remove);
    expect(document.querySelector(".preview-thumb")).toBeNull();
  });

  it("shows a PDF as a chip with the same button", () => {
    render(<Composer onSend={vi.fn()} busy={false} />);
    pick(new File(["%PDF"], "bill.pdf", { type: "application/pdf" }));
    expect(screen.getByText("bill.pdf")).toBeTruthy();
    expect(document.querySelector(".preview-thumb")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove the file" }));
    expect(screen.queryByText("bill.pdf")).toBeNull();
  });
});

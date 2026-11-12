import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer, COUNTER_FROM, MAX_CHARS, MAX_FILE_BYTES } from "./Composer";

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

  it("does not send on Enter with a coarse pointer; the button sends", () => {
    pointer(false);
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("hello", null);
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
});

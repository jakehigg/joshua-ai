import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer, COUNTER_FROM, MAX_CHARS, MAX_FILE_BYTES, MAX_ROWS } from "./Composer";

// `keys(true)` is a device with a keyboard of its own, `keys(false)` a touch
// screen alone. `primary` is the pointer of the device: a tablet in a keyboard
// case has a keyboard and a coarse primary pointer, `keys(true, false)`.
function keys(present: boolean, primary = present) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches:
      query === "(any-pointer: fine)" ? present : query === "(pointer: fine)" ? primary : false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

function setup() {
  const onSend = vi.fn().mockReturnValue(true);
  render(<Composer onSend={onSend} />);
  const box = screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement;
  const send = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
  return { onSend, box, send };
}

describe("Composer", () => {
  beforeEach(() => keys(true));
  afterEach(cleanup);

  it("sends on Enter with a keyboard, then empties and focuses the box", () => {
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

  it("adds a new line on Enter on a touch screen: no send, no prevented default", () => {
    keys(false);
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    const notPrevented = fireEvent.keyDown(box, { key: "Enter" });
    expect(notPrevented).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
    expect(box.getAttribute("enterkeyhint")).toBe("enter");
  });

  it("sends with the button on a touch screen", () => {
    keys(false);
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("hello", null);
  });

  it("leaves Shift+Enter to the browser on touch: no send, no prevented default", () => {
    keys(false);
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    const notPrevented = fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(notPrevented).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("sends on Enter on a tablet: a coarse pointer, and a keyboard beside it", () => {
    keys(true, false);
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    const prevented = fireEvent.keyDown(box, { key: "Enter" });
    expect(prevented).toBe(false);
    expect(onSend).toHaveBeenCalledWith("hello", null);
    expect(box.getAttribute("enterkeyhint")).toBe("send");
  });

  it("does not send on Enter during an IME composition", () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
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

  it("sends a text message while a turn runs: it waits for the turn", () => {
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "hello" } });
    expect(send.disabled).toBe(false);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello", null);
    expect(box.value).toBe("");
  });

  it("sends nothing with an empty box", () => {
    const { onSend, box, send } = setup();
    fireEvent.change(box, { target: { value: "  " } });
    expect(send.disabled).toBe(true);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
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
  beforeEach(() => keys(true));
  afterEach(cleanup);

  function pick(file: File) {
    const input = screen.getByLabelText(
      "Attach a photo, a PDF, or a text file",
    ) as HTMLInputElement;
    // jsdom has no file picker; set the list the input would have.
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    fireEvent.change(input);
    return input;
  }

  it("takes a photo and sends it with the text, then empties the chip", () => {
    const { onSend, box } = setup();
    const photo = new File(["pixels"], "photo.png", { type: "image/png" });
    pick(photo);
    expect(screen.getByAltText("photo.png")).toBeTruthy();
    fireEvent.change(box, { target: { value: "what is on this?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("what is on this?", photo);
    expect(screen.queryByAltText("photo.png")).toBeNull();
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

  it("keeps the paperclip live, so a message that waits for a turn carries a file", () => {
    const { send } = setup();
    const input = screen.getByLabelText(
      "Attach a photo, a PDF, or a text file",
    ) as HTMLInputElement;
    expect(input.disabled).toBe(false);
    pick(new File(["pixels"], "photo.png", { type: "image/png" }));
    expect(send.disabled).toBe(false);
  });

  it("takes images, PDFs and text files, by type and by extension", () => {
    setup();
    const input = screen.getByLabelText(
      "Attach a photo, a PDF, or a text file",
    ) as HTMLInputElement;
    expect(input.accept).toBe(
      "image/*,application/pdf,text/*,.txt,.csv,.tsv,.md,.markdown,.json",
    );
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
  beforeEach(() => keys(true));
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
    render(<Composer onSend={vi.fn()} />);
    pick(new File(["x"], "photo.png", { type: "image/png" }));
    const thumb = document.querySelector(".preview-thumb") as HTMLElement;
    expect(thumb.querySelector("img")?.getAttribute("src")).toBe("blob:p");
    const remove = thumb.querySelector("button.chip-remove") as HTMLButtonElement;
    fireEvent.click(remove);
    expect(document.querySelector(".preview-thumb")).toBeNull();
  });

  it("shows a PDF as a chip with the same button", () => {
    render(<Composer onSend={vi.fn()} />);
    pick(new File(["%PDF"], "bill.pdf", { type: "application/pdf" }));
    expect(screen.getByText("bill.pdf")).toBeTruthy();
    expect(document.querySelector(".preview-thumb")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove the file" }));
    expect(screen.queryByText("bill.pdf")).toBeNull();
  });
});

describe("Composer paste", () => {
  beforeEach(() => keys(true));
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  /** A paste of one file, as the browser gives it: an item and the file list. */
  function clipboard(file: File | null) {
    const items = file ? [{ kind: "file", type: file.type, getAsFile: () => file }] : [];
    return { items, files: file ? [file] : [] };
  }

  function paste(target: Element, data: unknown) {
    return fireEvent.paste(target, { clipboardData: data });
  }

  it("attaches a pasted screenshot, names it, and previews it", () => {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:p", revokeObjectURL: vi.fn() }));
    const { onSend, box } = setup();
    const shot = new File(["pixels"], "", { type: "image/png" });
    const prevented = !paste(box, clipboard(shot));
    expect(prevented).toBe(true);
    expect(document.querySelector(".preview-thumb img")?.getAttribute("src")).toBe("blob:p");
    fireEvent.keyDown(box, { key: "Enter" });
    const sent = onSend.mock.calls[0]?.[1] as File;
    expect(sent.name).toMatch(/^pasted-\d{8}-\d{6}\.png$/);
    expect(sent.type).toBe("image/png");
  });

  it("replaces the attached file, because one message takes one file", () => {
    setup();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const bill = new File(["%PDF"], "bill.pdf", { type: "application/pdf" });
    const note = new File(["%PDF"], "note.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [bill] } });
    // The form takes the paste too, so one with the box unfocused lands.
    paste(document.querySelector("form.composer") as Element, clipboard(note));
    expect(screen.queryByText("bill.pdf")).toBeNull();
    expect(screen.getByText("note.pdf")).toBeTruthy();
  });

  it("refuses a pasted file over the limit and attaches nothing", () => {
    const { box } = setup();
    const big = new File(["x"], "", { type: "image/png" });
    Object.defineProperty(big, "size", { value: MAX_FILE_BYTES + 1 });
    paste(box, clipboard(big));
    expect(screen.getByRole("alert").textContent).toMatch(/^pasted-\d{8}-\d{6}\.png is too large/);
    expect(document.querySelector(".preview-thumb")).toBeNull();
    expect(document.querySelector(".file-chip")).toBeNull();
  });

  it("refuses a pasted file of another type", () => {
    const { box } = setup();
    paste(box, clipboard(new File(["x"], "notes.zip", { type: "application/zip" })));
    expect(screen.getByRole("alert").textContent).toBe(
      "notes.zip is not a photo, a PDF, or a text file",
    );
    expect(document.querySelector(".file-chip")).toBeNull();
  });

  it("leaves a text paste alone: no attachment, no prevented default", () => {
    const { box } = setup();
    const pdf = new File(["%PDF"], "bill.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdf] } });
    const notPrevented = paste(box, { items: [{ kind: "string", type: "text/plain" }], files: [] });
    expect(notPrevented).toBe(true);
    expect(document.querySelector(".file-chip .file-name")?.textContent).toBe("bill.pdf");
  });

  it("takes the loaded draft once, with the caret after the text", () => {
    const onSend = vi.fn().mockReturnValue(true);
    const onDraft = vi.fn();
    const { rerender } = render(
      <Composer onSend={onSend} draft={null} onDraft={onDraft} />,
    );
    const box = screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement;
    expect(box.value).toBe("");

    rerender(<Composer onSend={onSend} draft="half a message" onDraft={onDraft} />);
    expect(box.value).toBe("half a message");
    expect(box.selectionStart).toBe("half a message".length);
    expect(document.activeElement).toBe(box);

    // The box is the person's now: a second draft of the server never
    // overwrites what they are typing.
    fireEvent.change(box, { target: { value: "half a message, and more" } });
    rerender(<Composer onSend={onSend} draft="something else" onDraft={onDraft} />);
    expect(box.value).toBe("half a message, and more");
    expect(onDraft).toHaveBeenLastCalledWith("half a message, and more");
  });

  it("clears the draft on a send", () => {
    const onSend = vi.fn().mockReturnValue(true);
    const onDraft = vi.fn();
    render(<Composer onSend={onSend} draft="" onDraft={onDraft} />);
    const box = screen.getByLabelText("Message to Joshua") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello", null);
    expect(onDraft).toHaveBeenLastCalledWith("");
  });
});

// A browser gives a CSV or a Markdown file an empty or an odd MIME type, so
// the three ways in — the paperclip, a paste and a drop — match on the
// extension as well as on the type.
describe("Composer text files", () => {
  beforeEach(() => keys(true));
  afterEach(cleanup);

  /** One file on a clipboard or on a drag, as the browser gives it. */
  function transfer(file: File) {
    return { items: [{ kind: "file", type: file.type, getAsFile: () => file }], files: [file] };
  }

  function pick(file: File) {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    fireEvent.change(input);
  }

  function paste(file: File) {
    const box = screen.getByLabelText("Message to Joshua");
    fireEvent.paste(box, { clipboardData: transfer(file) });
  }

  function drop(file: File) {
    const form = document.querySelector("form.composer") as Element;
    fireEvent.drop(form, { dataTransfer: transfer(file) });
  }

  const ways: [string, (file: File) => void][] = [
    ["the paperclip", pick],
    ["a paste", paste],
    ["a drop", drop],
  ];

  for (const [way, attach] of ways) {
    it(`takes a .csv with no MIME type through ${way}`, () => {
      const { onSend, send } = setup();
      const csv = new File(["title,pages\n"], "books.csv", { type: "" });
      attach(csv);
      expect(screen.getByText("books.csv")).toBeTruthy();
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.click(send);
      expect(onSend).toHaveBeenCalledWith("", csv);
    });

    it(`takes a .md with no MIME type through ${way}`, () => {
      const { onSend, send } = setup();
      const markdown = new File(["# notes\n"], "notes.md", { type: "" });
      attach(markdown);
      expect(screen.getByText("notes.md")).toBeTruthy();
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.click(send);
      expect(onSend).toHaveBeenCalledWith("", markdown);
    });

    it(`refuses a .zip and an .exe through ${way}, each with a message`, () => {
      setup();
      attach(new File(["PK"], "notes.zip", { type: "application/zip" }));
      expect(screen.getByRole("alert").textContent).toBe(
        "notes.zip is not a photo, a PDF, or a text file",
      );
      expect(document.querySelector(".file-chip")).toBeNull();
      attach(new File(["MZ"], "setup.exe", { type: "" }));
      expect(screen.getByRole("alert").textContent).toBe(
        "setup.exe is not a photo, a PDF, or a text file",
      );
      expect(document.querySelector(".file-chip")).toBeNull();
    });
  }

  it("takes a dropped file off the browser, so the tab does not open it", () => {
    setup();
    const form = document.querySelector("form.composer") as Element;
    const csv = new File(["title,pages\n"], "books.csv", { type: "" });
    const overNotAllowed = fireEvent.dragOver(form, { dataTransfer: transfer(csv) });
    const dropNotAllowed = fireEvent.drop(form, { dataTransfer: transfer(csv) });
    expect(overNotAllowed).toBe(false);
    expect(dropNotAllowed).toBe(false);
  });

  it("leaves a drag of text to the browser", () => {
    setup();
    const form = document.querySelector("form.composer") as Element;
    const text = { items: [{ kind: "string", type: "text/plain" }], files: [] };
    expect(fireEvent.dragOver(form, { dataTransfer: text })).toBe(true);
    expect(fireEvent.drop(form, { dataTransfer: text })).toBe(true);
    expect(document.querySelector(".file-chip")).toBeNull();
  });
});

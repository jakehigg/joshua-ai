// The text box and the Send button, in one rounded pill: the paperclip inside
// at the left, the round send button (an up arrow) inside at the right.
//
// On a device with a hardware keyboard, Enter sends and Shift+Enter adds a new
// line. A touch keyboard has no Shift+Enter, so there the return key adds a
// new line and the send button sends. The button sends in every case, and
// Enter does not send while an IME composition is in progress.
// The box is one line when empty and grows to MAX_ROWS rows, then scrolls. The hidden file input is out of the tab order, so the keyboard's
// "next field" arrow on a phone does not stop at it. A text
// over MAX_CHARS characters cannot be sent; the counter shows past
// COUNTER_FROM characters. The length is counted as the backend counts it:
// code points, after trim.
//
// The paperclip takes one photo, one PDF, or one text file (.txt, .csv,
// .tsv, .md, .markdown, .json). The chosen file shows as a chip
// above the box, with a button to take it off. A file over MAX_FILE_BYTES
// is not attached and says so; the backend holds the same limit. With a
// file attached, a message with no text can be sent.
//
// Cmd/Ctrl+V with a file on the clipboard attaches it, and so does a file
// dropped on the composer, as the paperclip does: the same types, the same
// size cap, the same preview. A screenshot of the operating system arrives as
// an image/png file with no name of its own, so it gets one,
// pasted-YYYYMMDD-HHMMSS.png. A paste with no file is a text paste and stays
// one, and a drag with no file is left to the browser.
//
// A message can be written while Joshua is still working on a turn: it is
// taken and waits for that turn (useTurn.ts), and the box is emptied as
// usual. A file waits with it, so the paperclip, the paste and the send
// button work the same while a turn runs.
//
// Text that is not sent is a draft, kept on the server for this thread and
// this person (useComposerDraft.ts). The box takes the loaded draft once and
// puts the caret after it, each change goes back as the new draft, and a send
// clears it.

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { useMedia } from "./viewport";

export const MAX_CHARS = 8000;
export const COUNTER_FROM = 7000;
// One file per message, as the backend's TURN_MAX_BYTES allows.
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
// The box stops growing at this many lines and scrolls (index.css, max-height).
export const MAX_ROWS = 5;
// The extensions of a text file. A browser gives a CSV or a Markdown file an
// empty or an odd MIME type, so the extension decides as well as the type.
export const TEXT_EXTENSIONS = [".txt", ".csv", ".tsv", ".md", ".markdown", ".json"];
export const FILE_ACCEPT = `image/*,application/pdf,text/*,${TEXT_EXTENSIONS.join(",")}`;
// What the composer calls the types it takes, in the hint and in a refusal.
export const FILE_KINDS = "a photo, a PDF, or a text file";

export function charCount(text: string): number {
  return Array.from(text.trim()).length;
}

/** True for a file that FILE_ACCEPT takes: a photo, a PDF, or a text file. */
function accepted(type: string, name: string): boolean {
  if (type.startsWith("image/") || type === "application/pdf" || type.startsWith("text/")) {
    return true;
  }
  const lower = name.toLowerCase();
  return TEXT_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** The name of a pasted file that has none: pasted-YYYYMMDD-HHMMSS.<ext>. */
function pastedName(type: string, now = new Date()): string {
  const two = (n: number) => String(n).padStart(2, "0");
  const day = `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}`;
  const time = `${two(now.getHours())}${two(now.getMinutes())}${two(now.getSeconds())}`;
  const ext = (type.split("/")[1] ?? "bin").split(/[;+]/)[0] ?? "bin";
  return `pasted-${day}-${time}.${ext}`;
}

/** The file of a paste or a drop: the first entry of the transfer that is a file. */
function transferFile(data: DataTransfer | null): File | null {
  if (!data) return null;
  const entry = Array.from(data.items ?? []).find((item) => item.kind === "file");
  return entry?.getAsFile() ?? Array.from(data.files ?? [])[0] ?? null;
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
      <path fill="currentColor" d="M12 4l7 7-1.4 1.4L13 7.8V20h-2V7.8l-4.6 4.6L5 11z" />
    </svg>
  );
}

function ClipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M16.5 6.5v8.75a4.5 4.5 0 0 1-9 0V7a3 3 0 0 1 6 0v8.25a1.5 1.5 0 0 1-3 0V7.5H9v7.75a3 3 0 0 0 6 0V7a4.5 4.5 0 0 0-9 0v8.25a6 6 0 0 0 12 0V6.5h-1.5Z"
      />
    </svg>
  );
}

// The chosen file above the box: a photo as a small rounded thumbnail with the
// remove button on its corner, anything else as a chip with the same button.
function Preview({ file, onRemove }: { file: File; onRemove: () => void }) {
  const url = useMemo(
    () =>
      file.type.startsWith("image/") && typeof URL.createObjectURL === "function"
        ? URL.createObjectURL(file)
        : null,
    [file],
  );
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  const remove = (
    <button type="button" className="chip-remove" onClick={onRemove} aria-label="Remove the file">
      ✕
    </button>
  );
  if (url) {
    return (
      <div className="attached">
        <span className="preview-thumb">
          <img src={url} alt={file.name} />
          {remove}
        </span>
      </div>
    );
  }
  return (
    <div className="attached">
      <span className="file-chip">
        <span aria-hidden="true">📎</span>
        <span className="file-name">{file.name}</span>
        {remove}
      </span>
    </div>
  );
}

interface Props {
  // Returns true when the message was sent; the box is then emptied.
  onSend: (text: string, file?: File | null) => boolean;
  // The draft of this thread, from the server, or null while it loads. The
  // box takes it once, with the caret after the text.
  draft?: string | null;
  // Keeps what is in the box as the draft of this thread; "" clears it.
  onDraft?: (text: string) => void;
}

export function Composer({ onSend, draft = null, onDraft }: Props) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  // The draft fills the box once. After that the box is the person's.
  const tookDraft = useRef(false);

  const change = (value: string) => {
    setText(value);
    onDraft?.(value);
  };

  useEffect(() => {
    if (draft === null || tookDraft.current) return;
    tookDraft.current = true;
    if (draft === "") return;
    setText(draft);
    const el = ref.current;
    if (el) {
      el.focus();
      el.setSelectionRange(draft.length, draft.length);
    }
  }, [draft]);

  const count = charCount(text);
  const tooLong = count > MAX_CHARS;
  // While Joshua works, the message is still taken: it waits for the running
  // turn, with its file (useTurn.ts).
  const canSend = !tooLong && (count > 0 || file !== null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Empty: no inline height, so the CSS one-line height holds. Otherwise
    // the text height; the CSS max-height stops it at MAX_ROWS rows.
    if (text === "") {
      el.style.height = "";
      return;
    }
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  const submit = () => {
    if (!canSend) return;
    if (onSend(text, file)) {
      change("");
      setFile(null);
      setFileError(null);
    }
    ref.current?.focus();
  };

  // Attaches the file under name, or says why it is not attached. A paste
  // gives a name to a file that has none.
  const attach = (picked: File, name = picked.name) => {
    if (picked.size > MAX_FILE_BYTES) {
      setFile(null);
      setFileError(`${name} is too large; the limit is ${MAX_FILE_BYTES / 1024 / 1024} MB`);
      return;
    }
    setFileError(null);
    setFile(name === picked.name ? picked : new File([picked], name, { type: picked.type }));
  };

  // Takes a picked, pasted or dropped file: a file with no name of its own
  // gets one, and a type the composer does not take is refused with a message.
  const take = (picked: File) => {
    const name = picked.name === "" ? pastedName(picked.type) : picked.name;
    if (!accepted(picked.type, name)) {
      setFile(null);
      setFileError(`${name} is not ${FILE_KINDS}`);
      return;
    }
    attach(picked, name);
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0] ?? null;
    // Empty the input, so the same file can be chosen again after Remove.
    e.target.value = "";
    if (!picked) return;
    take(picked);
  };

  // The handler sits on the box and on the form, so a paste with the box
  // unfocused lands too; the box's paste reaches the form as well, and
  // defaultPrevented keeps the file from being taken twice.
  const onPaste = (e: ClipboardEvent<HTMLElement>) => {
    if (e.defaultPrevented) return;
    const pasted = transferFile(e.clipboardData);
    // No file: a text paste, which the browser does as usual.
    if (!pasted) return;
    e.preventDefault();
    take(pasted);
  };

  // A file dragged onto the composer attaches on the drop. The dragover is
  // taken as well, because a browser that is not told the drop is wanted
  // opens the file in the tab instead.
  const onDragOver = (e: DragEvent<HTMLElement>) => {
    if (Array.from(e.dataTransfer?.items ?? []).some((item) => item.kind === "file")) {
      e.preventDefault();
    }
  };

  const onDrop = (e: DragEvent<HTMLElement>) => {
    if (e.defaultPrevented) return;
    const dropped = transferFile(e.dataTransfer);
    // No file: a drag of text, which the browser drops into the box as usual.
    if (!dropped) return;
    e.preventDefault();
    take(dropped);
  };

  const removeFile = () => {
    setFile(null);
    setFileError(null);
    ref.current?.focus();
  };

  // A fine pointer anywhere — a mouse, a trackpad, the trackpad of a keyboard
  // case — means a keyboard of its own, with a Shift key. It is not always the
  // primary pointer: a tablet in a keyboard case is `(pointer: coarse)` and
  // `(any-pointer: fine)` at once.
  const keyboard = useMedia("(any-pointer: fine)");

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (!keyboard) return;
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    submit();
  };

  return (
    <form
      className="composer"
      onSubmit={onSubmit}
      onPaste={onPaste}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      {file && <Preview file={file} onRemove={removeFile} />}
      <div className="composer-inner">
        <label className="attach" title={`Attach ${FILE_KINDS}`}>
          <ClipIcon />
          <span className="visually-hidden">Attach {FILE_KINDS}</span>
          <input type="file" accept={FILE_ACCEPT} onChange={onPick} tabIndex={-1} />
        </label>
        <textarea
          ref={ref}
          className="composer-input"
          rows={1}
          value={text}
          onChange={(e) => change(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder="Message Joshua"
          aria-label="Message to Joshua"
          aria-invalid={tooLong || undefined}
          aria-describedby={count > COUNTER_FROM ? "composer-count" : undefined}
          enterKeyHint={keyboard ? "send" : "enter"}
        />
        <button type="submit" className="send" disabled={!canSend} aria-label="Send">
          <SendIcon />
        </button>
      </div>
      {fileError && (
        <div className="count over" role="alert">
          {fileError}
        </div>
      )}
      {count > COUNTER_FROM && (
        <div id="composer-count" className={tooLong ? "count over" : "count"} role="status">
          {count} / {MAX_CHARS}
          {tooLong && " — too long to send"}
        </div>
      )}
    </form>
  );
}

// The text box and the Send button, in one rounded pill: the paperclip inside
// at the left, the round send button (an up arrow) inside at the right.
//
// On a fine pointer (a mouse or trackpad), Enter sends and Shift+Enter
// adds a new line. On a coarse pointer (a touch screen), Enter adds a new
// line and only the button sends. The box grows from 1 to MAX_ROWS rows, then
// scrolls. The hidden file input is out of the tab order, so the keyboard's
// "next field" arrow on a phone does not stop at it. A text
// over MAX_CHARS characters cannot be sent; the counter shows past
// COUNTER_FROM characters. The length is counted as the backend counts it:
// code points, after trim.
//
// The paperclip takes one photo or one PDF. The chosen file shows as a chip
// above the box, with a button to take it off. A file over MAX_FILE_BYTES
// is not attached and says so; the backend holds the same limit. With a
// file attached, a message with no text can be sent.

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

export const MAX_CHARS = 8000;
export const COUNTER_FROM = 7000;
// One file per message, as the backend's TURN_MAX_BYTES allows.
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
// The box stops growing at this many lines and scrolls (index.css, max-height).
export const MAX_ROWS = 5;
export const FILE_ACCEPT = "image/*,application/pdf";

function finePointer(): boolean {
  if (typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(pointer: fine)").matches;
}

export function charCount(text: string): number {
  return Array.from(text.trim()).length;
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
  busy: boolean;
}

export function Composer({ onSend, busy }: Props) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  const count = charCount(text);
  const tooLong = count > MAX_CHARS;
  const canSend = !busy && !tooLong && (count > 0 || file !== null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // The CSS max-height holds the box at 8 rows; past that it scrolls.
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  const submit = () => {
    if (!canSend) return;
    if (onSend(text, file)) {
      setText("");
      setFile(null);
      setFileError(null);
    }
    ref.current?.focus();
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0] ?? null;
    // Empty the input, so the same file can be chosen again after Remove.
    e.target.value = "";
    if (!picked) return;
    if (picked.size > MAX_FILE_BYTES) {
      setFile(null);
      setFileError(`${picked.name} is too large; the limit is ${MAX_FILE_BYTES / 1024 / 1024} MB`);
      return;
    }
    setFileError(null);
    setFile(picked);
  };

  const removeFile = () => {
    setFile(null);
    setFileError(null);
    ref.current?.focus();
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    if (!finePointer()) return;
    e.preventDefault();
    submit();
  };

  return (
    <form className="composer" onSubmit={onSubmit}>
      {file && <Preview file={file} onRemove={removeFile} />}
      <div className="composer-inner">
        <label className="attach" title="Attach a photo or a PDF">
          <ClipIcon />
          <span className="visually-hidden">Attach a photo or a PDF</span>
          <input type="file" accept={FILE_ACCEPT} onChange={onPick} disabled={busy} tabIndex={-1} />
        </label>
        <textarea
          ref={ref}
          className="composer-input"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Message Joshua"
          aria-label="Message to Joshua"
          aria-invalid={tooLong || undefined}
          aria-describedby={count > COUNTER_FROM ? "composer-count" : undefined}
          enterKeyHint="send"
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

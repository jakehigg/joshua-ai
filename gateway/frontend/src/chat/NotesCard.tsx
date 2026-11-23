// The notes of a workspace. The notes are the memory of the workspace: Joshua
// reads them on every turn, and a reset of his session keeps them.
//
// NotesCard stands first in the item panel: the name, the size, and the first
// lines. A click opens NotesPanel in the item sheet, where a document opens
// too. There the person reads the notes whole, edits the whole text in a
// field the size of the sheet, or puts a comment on one line with its `+`.
// A comment goes into the thread as a message of the person, with the line it
// is about, so Joshua changes the notes himself with his notes tools.

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { changeWorkspace, type Workspace } from "../api/workspaces";
import { errorText } from "../api/client";
import { useDocumentPanel } from "./DocumentPanel";
import { Markdown } from "./Markdown";

/** What a notes view needs from the chat screen. */
export interface NotesProps {
  workspace: Workspace;
  onSaved: (w: Workspace) => void;
  /** Send one message into the thread of the workspace. */
  onAsk: (text: string) => void;
}

/** The title of the notes, in the sheet and in the panel. */
export const NOTES_TITLE = "Workspace notes";

/** The message of a comment on one line of the notes. */
export function commentMessage(line: string, comment: string): string {
  return `Comment on the workspace notes, on the line "${line.trim()}": ${comment.trim()}`;
}

/** The message of a comment on the notes as a whole. */
export function wholeMessage(comment: string): string {
  return `Update the workspace notes: ${comment.trim()}`;
}

const PREVIEW_LINES = 4;

function preview(notes: string): string[] {
  return notes
    .split("\n")
    .map((line) => line.replace(/^#+\s*/, "").replace(/^[-*]\s+/, "").trim())
    .filter((line) => line !== "")
    .slice(0, PREVIEW_LINES);
}

export function NotesCard({ workspace }: { workspace: Workspace }) {
  const panel = useDocumentPanel();
  const notes = workspace.notes.trim();
  const active = panel.view?.type === "notes";
  return (
    <section className="ws-notes-card" aria-label={NOTES_TITLE}>
      <button
        type="button"
        className={`ws-notes-open${active ? " active" : ""}`}
        onClick={() => panel.open({ type: "notes" })}
      >
        <span className="ws-notes-head">
          <span className="ws-notes-name">{NOTES_TITLE}</span>
          <span className="item-count">{notes ? `${workspace.notes.length} chars` : "empty"}</span>
        </span>
        {notes ? (
          <span className="ws-notes-preview">
            {preview(workspace.notes).map((line, i) => (
              <span key={i}>{line}</span>
            ))}
          </span>
        ) : (
          <span className="j-meta">No notes yet. Open them to write the first ones.</span>
        )}
      </button>
    </section>
  );
}

type Mode = "read" | "edit";

/** The notes in the item sheet: read with a `+` on each line, or edit them whole. */
export function NotesPanel({
  notes: { workspace, onSaved, onAsk },
  onClose,
  fullToggle,
}: {
  notes: NotesProps;
  onClose: () => void;
  /** The full-screen toggle of the sheet, drawn in the header. */
  fullToggle?: ReactNode;
}) {
  const [mode, setMode] = useState<Mode>(workspace.notes.trim() ? "read" : "edit");
  const [text, setText] = useState(workspace.notes);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  // The line the comment box is open under: its index, or -1 for the notes
  // as a whole, or null when no box is open.
  const [commenting, setCommenting] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [sent, setSent] = useState<number[]>([]);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const lines = workspace.notes.split("\n");
  const dirty = text !== workspace.notes;

  const save = (e?: FormEvent) => {
    e?.preventDefault();
    setSaving(true);
    setError("");
    changeWorkspace(workspace.id, { notes: text })
      .then((w) => {
        onSaved(w);
        setMode("read");
      })
      .catch((exc: unknown) => setError(errorText(exc, "the notes were not saved")))
      .finally(() => setSaving(false));
  };

  const send = (e: FormEvent) => {
    e.preventDefault();
    if (commenting === null || comment.trim() === "") return;
    onAsk(commenting < 0 ? wholeMessage(comment) : commentMessage(lines[commenting] ?? "", comment));
    setSent((s) => [...s, commenting]);
    setComment("");
    setCommenting(null);
  };

  const box = (at: number) =>
    commenting === at && (
      <form className="ws-notes-comment" onSubmit={send}>
        <textarea
          aria-label={at < 0 ? "What should change in the notes" : "Your comment on this line"}
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={at < 0 ? "What should change in the notes?" : "What should change in this line?"}
          autoFocus
        />
        <div className="ws-notes-actions">
          <button type="button" onClick={() => setCommenting(null)}>
            Cancel
          </button>
          <button type="submit" disabled={comment.trim() === ""}>
            Send to Joshua
          </button>
        </div>
      </form>
    );

  return (
    <aside className="doc-panel ws-notes-panel" aria-label={`${NOTES_TITLE}, the notes`}>
      <header className="doc-panel-header">
        <span className="doc-panel-title">{NOTES_TITLE}</span>
        {mode === "read" ? (
          <>
            <button type="button" className="ws-notes-tool" onClick={() => setCommenting(-1)}>
              Ask Joshua
            </button>
            <button
              type="button"
              className="ws-notes-tool"
              onClick={() => {
                setText(workspace.notes);
                setMode("edit");
              }}
            >
              Edit notes
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="ws-notes-tool"
              disabled={saving}
              onClick={() => {
                setText(workspace.notes);
                setError("");
                setMode("read");
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="ws-notes-tool primary"
              disabled={saving || !dirty}
              onClick={() => save()}
            >
              Save notes
            </button>
          </>
        )}
        {fullToggle}
        <button
          type="button"
          ref={closeRef}
          className="doc-panel-close"
          aria-label="Close the notes"
          onClick={onClose}
        >
          ✕
        </button>
      </header>
      {mode === "edit" ? (
        <form className="ws-notes-editor" onSubmit={save}>
          <textarea
            aria-label={NOTES_TITLE}
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck
            autoFocus
          />
          <p className="j-meta ws-notes-meta">
            {text.length} characters. Joshua keeps the sections Rules, Repositories, and What I can do.
          </p>
          {error && (
            <p className="j-error" role="alert">
              {error}
            </p>
          )}
        </form>
      ) : (
        <div className="ws-notes-read">
          <p className="j-meta ws-notes-meta">
            Comment on a line with +. The comment goes to Joshua in this thread, and he changes the notes.
          </p>
          {box(-1)}
          {sent.includes(-1) && <p className="j-meta ws-notes-sent">Sent to Joshua.</p>}
          {lines.map((line, i) =>
            line.trim() === "" ? (
              <div key={i} className="ws-notes-gap" />
            ) : (
              <div key={i} className={`ws-notes-line${commenting === i ? " active" : ""}`}>
                <button
                  type="button"
                  className="ws-notes-plus"
                  aria-label={`Comment on line ${i + 1}`}
                  onClick={() => {
                    setComment("");
                    setCommenting(commenting === i ? null : i);
                  }}
                >
                  +
                </button>
                <div className="ws-notes-text">
                  <Markdown text={line} />
                  {sent.includes(i) && <span className="j-meta ws-notes-sent">Sent to Joshua.</span>}
                  {box(i)}
                </div>
              </div>
            ),
          )}
        </div>
      )}
    </aside>
  );
}

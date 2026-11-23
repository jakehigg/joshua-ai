// The notes of a workspace, at the top of the item panel. The notes are the
// memory of the workspace: Joshua reads them on every turn, and a reset of
// his session keeps them. So they stand first, under their own name, and a
// person reads them whole, edits them in place, or asks Joshua to change
// them. "Ask Joshua" sends the request into the thread as a message of the
// person, so Joshua changes the notes himself with his notes tools.

import { useState, type FormEvent } from "react";
import { changeWorkspace, type Workspace } from "../api/workspaces";
import { errorText } from "../api/client";
import { GrowTextarea } from "./GrowTextarea";
import { Markdown } from "./Markdown";

/** The words that start the message of "Ask Joshua". */
export const ASK_PREFIX = "Update the workspace notes: ";

type Mode = "read" | "edit" | "ask";

export function NotesCard({
  workspace,
  onSaved,
  onAsk,
}: {
  workspace: Workspace;
  onSaved: (w: Workspace) => void;
  /** Send one message into the thread of the workspace. */
  onAsk: (text: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [mode, setMode] = useState<Mode>("read");
  const [text, setText] = useState(workspace.notes);
  const [ask, setAsk] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const notes = workspace.notes.trim();

  const startEdit = () => {
    setText(workspace.notes);
    setError("");
    setMode("edit");
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
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
    const words = ask.trim();
    if (words === "") return;
    onAsk(ASK_PREFIX + words);
    setAsk("");
    setMode("read");
  };

  return (
    <section className="ws-notes-card" aria-label="Workspace notes">
      <h3 className="rail-label item-group">
        <button
          type="button"
          className="item-group-toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          Workspace notes
          <span className="item-count">{notes ? `${workspace.notes.length} chars` : "empty"}</span>
        </button>
      </h3>
      {open && mode === "read" && (
        <>
          <div className="ws-notes-body">
            {notes ? (
              <Markdown text={workspace.notes} />
            ) : (
              <p className="j-meta">
                No notes yet. Joshua keeps the rules, the repositories, and what he may do here.
              </p>
            )}
          </div>
          <div className="ws-notes-actions">
            <button type="button" onClick={startEdit}>
              Edit notes
            </button>
            <button type="button" onClick={() => setMode("ask")}>
              Ask Joshua
            </button>
          </div>
        </>
      )}
      {open && mode === "edit" && (
        <form className="ws-notes-form" onSubmit={save}>
          <GrowTextarea
            className="ws-notes-edit"
            aria-label="Workspace notes"
            rows={12}
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoFocus
          />
          {error && (
            <p className="j-error" role="alert">
              {error}
            </p>
          )}
          <div className="ws-notes-actions">
            <button type="button" onClick={() => setMode("read")} disabled={saving}>
              Cancel
            </button>
            <button type="submit" disabled={saving || text === workspace.notes}>
              Save notes
            </button>
          </div>
        </form>
      )}
      {open && mode === "ask" && (
        <form className="ws-notes-form" onSubmit={send}>
          <GrowTextarea
            aria-label="What should change in the notes"
            rows={3}
            value={ask}
            onChange={(e) => setAsk(e.target.value)}
            placeholder="What should change? For example: add that joshua/developer is retired."
            autoFocus
          />
          <div className="ws-notes-actions">
            <button type="button" onClick={() => setMode("read")}>
              Cancel
            </button>
            <button type="submit" disabled={ask.trim() === ""}>
              Send to Joshua
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

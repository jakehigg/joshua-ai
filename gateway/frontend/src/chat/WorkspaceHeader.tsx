// The header of a workspace: its title and goal, and an edit drawer for the
// goal, the notes, the servers, the model, the notify setting and the status
// (pause, resume, archive).

import { useEffect, useState, type FormEvent } from "react";
import {
  changeWorkspace,
  getOptions,
  type Notify,
  type Options,
  type Workspace,
  type WorkspaceStatus,
} from "../api/workspaces";
import { GrowTextarea, scrollFieldIntoView } from "./GrowTextarea";
import { ServerChecklist } from "./ServerChecklist";

const NOTIFY: [Notify, string][] = [
  ["none", "Never"],
  ["questions", "Questions"],
  ["questions+finishes", "Questions and finishes"],
];

function EditDrawer({
  workspace,
  onSaved,
  onClose,
}: {
  workspace: Workspace;
  onSaved: (w: Workspace) => void;
  onClose: () => void;
}) {
  const [options, setOptions] = useState<Options | null>(null);
  const [title, setTitle] = useState(workspace.title);
  const [goal, setGoal] = useState(workspace.goal);
  const [notes, setNotes] = useState(workspace.notes);
  const [model, setModel] = useState(workspace.model ?? "");
  const [notify, setNotify] = useState<Notify>(workspace.notify);
  const [servers, setServers] = useState(workspace.servers);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    getOptions()
      .then((o) => live && setOptions(o))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const save = (patch: Parameters<typeof changeWorkspace>[1], close: boolean) => {
    setError("");
    changeWorkspace(workspace.slug, patch)
      .then((w) => {
        onSaved(w);
        if (close) onClose();
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "the change was not saved"));
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (title.trim() === "") return;
    save({ title: title.trim(), goal, notes, model, notify, servers }, true);
  };

  const setStatus = (status: WorkspaceStatus) => save({ status }, true);
  // The model of a workspace may be one that the list no longer holds.
  const models = [...new Set([...(options?.models ?? []), ...(model ? [model] : [])])];
  const known = new Set(options?.servers.map((s) => s.name));
  const shown = [
    ...(options?.servers ?? []),
    ...servers.filter((n) => options && !known.has(n)).map((n) => ({ name: n, description: n })),
  ];

  return (
    <div className="ws-overlay">
      <form className="ws-dialog" role="dialog" aria-modal="true" aria-label="Edit workspace" onSubmit={submit} onFocus={scrollFieldIntoView}>
        <h2>Edit workspace</h2>
        <div className="ws-body">
        <label className="j-field-label" htmlFor="ws-e-title">
          Title
        </label>
        <input id="ws-e-title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <label className="j-field-label" htmlFor="ws-e-goal">
          Goal
        </label>
        <GrowTextarea id="ws-e-goal" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} />
        <label className="j-field-label" htmlFor="ws-e-notes">
          Notes
        </label>
        <GrowTextarea id="ws-e-notes" rows={5} value={notes} onChange={(e) => setNotes(e.target.value)} />
        <label className="j-field-label" htmlFor="ws-e-model">
          Model
        </label>
        <select id="ws-e-model" value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">Default</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <label className="j-field-label" htmlFor="ws-e-notify">
          Messages to your phone
        </label>
        <select id="ws-e-notify" value={notify} onChange={(e) => setNotify(e.target.value as Notify)}>
          {NOTIFY.map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
        <ServerChecklist servers={shown} picked={servers} onChange={setServers} />
        {error && (
          <p className="j-error" role="alert">
            {error}
          </p>
        )}
        </div>
        <div className="ws-actions">
          {workspace.status === "active" ? (
            <button type="button" onClick={() => setStatus("paused")}>
              Pause
            </button>
          ) : (
            <button type="button" onClick={() => setStatus("active")}>
              Resume
            </button>
          )}
          {workspace.status !== "archived" && (
            <button type="button" onClick={() => setStatus("archived")}>
              Archive
            </button>
          )}
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="send" disabled={title.trim() === ""}>
            Save
          </button>
        </div>
      </form>
    </div>
  );
}

export function WorkspaceHeader({
  workspace,
  onChange,
  onItems,
}: {
  workspace: Workspace;
  onChange: (w: Workspace) => void;
  onItems: () => void;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <>
      <div className="title ws-title">
        <h1>{workspace.title}</h1>
        {workspace.status !== "active" && <span className="subtitle">{workspace.status}</span>}
        {workspace.goal && <span className="subtitle ws-goal">{workspace.goal}</span>}
      </div>
      <div className="ws-header-actions">
        <button type="button" className="items-toggle" onClick={onItems}>
          Items
        </button>
        <button type="button" onClick={() => setEditing(true)}>
          Edit
        </button>
      </div>
      {editing && (
        <EditDrawer workspace={workspace} onSaved={onChange} onClose={() => setEditing(false)} />
      )}
    </>
  );
}

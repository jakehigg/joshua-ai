// The new workspace dialog: title, goal, model, and the servers. It creates
// the workspace and hands it to onCreated, which opens it.

import { useEffect, useState, type FormEvent } from "react";
import { createWorkspace, getOptions, type Options, type Workspace } from "../api/workspaces";
import { ServerChecklist } from "./ServerChecklist";

export function NewWorkspace({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (workspace: Workspace) => void;
}) {
  const [options, setOptions] = useState<Options | null>(null);
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [model, setModel] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    getOptions()
      .then((o) => live && setOptions(o))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "no options"));
    return () => {
      live = false;
    };
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (title.trim() === "" || busy) return;
    setBusy(true);
    setError("");
    createWorkspace({
      title: title.trim(),
      ...(goal.trim() ? { goal: goal.trim() } : {}),
      ...(model ? { model } : {}),
      servers: picked,
    })
      .then(onCreated)
      .catch((err: unknown) => {
        setBusy(false);
        setError(err instanceof Error ? err.message : "the workspace was not created");
      });
  };

  return (
    <div className="ws-overlay">
      <form className="ws-dialog" role="dialog" aria-modal="true" aria-label="New workspace" onSubmit={submit}>
        <h2>New workspace</h2>
        <label className="j-field-label" htmlFor="ws-title">
          Title
        </label>
        <input id="ws-title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        <label className="j-field-label" htmlFor="ws-goal">
          Goal (optional)
        </label>
        <textarea id="ws-goal" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} />
        <label className="j-field-label" htmlFor="ws-model">
          Model
        </label>
        <select id="ws-model" value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">Default</option>
          {(options?.models ?? []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <ServerChecklist servers={options?.servers ?? []} picked={picked} onChange={setPicked} />
        {error && (
          <p className="j-error" role="alert">
            {error}
          </p>
        )}
        <div className="ws-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="send" disabled={title.trim() === "" || busy}>
            Create
          </button>
        </div>
      </form>
    </div>
  );
}

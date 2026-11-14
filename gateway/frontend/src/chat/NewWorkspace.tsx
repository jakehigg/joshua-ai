// The new workspace dialog: title, goal, model, the servers, and whether the
// workspace is shared. Shared shows the other people of the roster: the ones
// ticked write in the one thread of the workspace, which then lives at
// `/chat/w/shared/<slug>`, one address for everyone. That slug is taken from
// the title and is one for the whole instance, so a title whose slug is taken
// is refused (409) and the dialog asks for another one. The people are set
// here and never change. The dialog creates the workspace and hands it to
// onCreated, which opens it. On a phone it is a
// full-height sheet: the fields scroll in `ws-body`, and Cancel and Create
// stay in a footer above the keyboard (index.css).

import { useEffect, useState, type FormEvent } from "react";
import { ApiError, errorText } from "../api/client";
import {
  createWorkspace,
  getOptions,
  SHARED,
  slugOf,
  type Options,
  type Workspace,
} from "../api/workspaces";
import { GrowTextarea, scrollFieldIntoView } from "./GrowTextarea";
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
  const [shared, setShared] = useState(false);
  const [members, setMembers] = useState<string[]>([]);
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

  const slug = slugOf(title.trim());
  // A shared workspace with nobody in it is a workspace of one person, so
  // Create waits for at least one.
  const ready = title.trim() !== "" && !busy && (!shared || members.length > 0);
  const toggleMember = (id: string) =>
    setMembers(members.includes(id) ? members.filter((m) => m !== id) : [...members, id]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError("");
    createWorkspace({
      title: title.trim(),
      ...(goal.trim() ? { goal: goal.trim() } : {}),
      ...(model ? { model } : {}),
      servers: picked,
      ...(shared ? { members } : {}),
    })
      .then(onCreated)
      .catch((err: unknown) => {
        setBusy(false);
        setError(
          err instanceof ApiError && err.status === 409
            ? `A shared workspace called ${slug} exists. Pick another title.`
            : errorText(err, "the workspace was not created"),
        );
      });
  };

  return (
    <div className="ws-overlay">
      <form className="ws-dialog" role="dialog" aria-modal="true" aria-label="New workspace" onSubmit={submit} onFocus={scrollFieldIntoView}>
        <h2>New workspace</h2>
        <div className="ws-body">
        <label className="j-field-label" htmlFor="ws-title">
          Title
        </label>
        <input id="ws-title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        <label className="j-field-label" htmlFor="ws-goal">
          Goal (optional)
        </label>
        <GrowTextarea id="ws-goal" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} />
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
        <label className="ws-person">
          <input
            type="checkbox"
            checked={shared}
            onChange={(e) => {
              setShared(e.target.checked);
              if (!e.target.checked) setMembers([]);
            }}
          />
          <span>Shared</span>
        </label>
        {shared && (
          <fieldset className="ws-people">
            <legend className="j-field-label">People</legend>
            {(options?.people ?? []).length === 0 && (
              <p className="j-meta">Nobody else to share this with.</p>
            )}
            {(options?.people ?? []).map((person) => (
              <label key={person.id} className="ws-person">
                <input
                  type="checkbox"
                  checked={members.includes(person.id)}
                  onChange={() => toggleMember(person.id)}
                />
                <span>{person.name}</span>
              </label>
            ))}
            <p className="ws-link j-meta">
              Everyone in it writes in one thread, at /chat/w/{SHARED}/{slug}
            </p>
          </fieldset>
        )}
        <ServerChecklist servers={options?.servers ?? []} picked={picked} onChange={setPicked} />
        {error && (
          <p className="j-error" role="alert">
            {error}
          </p>
        )}
        </div>
        <div className="ws-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="send" disabled={!ready}>
            Create
          </button>
        </div>
      </form>
    </div>
  );
}

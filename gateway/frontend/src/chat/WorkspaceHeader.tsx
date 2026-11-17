// The header of a workspace: its title and goal, and an edit drawer for the
// goal, the notes, the servers, the model, the notify setting and the status
// (pause, resume, archive). The notify setting says when Joshua writes to the
// phone. Whether a device hears that is the device's own setting, one person's
// and not one workspace's, so it is on the Profile page and the drawer points
// there. A shared workspace adds "When Joshua may reach you
// here", the notify mode of the signed-in person: it governs the `notify_user`
// tool, it is theirs alone, and it saves as it changes. A shared workspace carries a two-person icon
// beside its title and names its people under it, "Jake, Mia". They are not in
// the drawer: they are set at the create and never change. A workspace Joshua
// is working for, or one that waits for a person, carries the charm of that
// beside the title too (WorkCharm.tsx).

import { useContext, useEffect, useState, type FormEvent } from "react";
import {
  changeWorkspace,
  getNotifyMode,
  getOptions,
  setNotifyMode,
  type Notify,
  type NotifyMode,
  type Options,
  type Workspace,
  type WorkspaceStatus,
} from "../api/workspaces";
import { errorText } from "../api/client";
import { SessionContext } from "../shell/Shell";
import { GrowTextarea, scrollFieldIntoView } from "./GrowTextarea";
import { ServerChecklist } from "./ServerChecklist";
import { SharedMark } from "./SharedMark";
import { WorkCharm } from "./WorkCharm";

const NOTIFY: [Notify, string][] = [
  ["none", "Never"],
  ["questions", "Questions"],
  ["questions+finishes", "Questions and finishes"],
];

const NOTIFY_MODE: [NotifyMode, string][] = [
  ["off", "Never"],
  ["decide", "When the thread needs me"],
  ["all", "Every message of another person"],
];

// The notify mode of the signed-in person in a shared workspace: when Joshua
// may reach them there, rather than wait for them to open the thread. It is
// one person's own setting, so it saves on its own, not with the form.
function NotifyModeField({ workspace }: { workspace: Workspace }) {
  const [mode, setMode] = useState<NotifyMode | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    getNotifyMode(workspace.id)
      .then((state) => live && setMode(state.mode))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [workspace]);

  if (mode === null) return null;

  const pick = (picked: NotifyMode) => {
    setMode(picked);
    setError("");
    setNotifyMode(workspace.id, picked)
      .then((state) => setMode(state.mode))
      .catch((e: unknown) => setError(errorText(e, "the setting was not saved")));
  };

  return (
    <>
      <label className="j-field-label" htmlFor="ws-e-notify-mode">
        When Joshua may reach you here
      </label>
      <select
        id="ws-e-notify-mode"
        value={mode}
        onChange={(e) => pick(e.target.value as NotifyMode)}
      >
        {NOTIFY_MODE.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
      {error && (
        <p className="j-error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

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
    changeWorkspace(workspace.id, patch)
      .then((w) => {
        onSaved(w);
        if (close) onClose();
      })
      .catch((e: unknown) => setError(errorText(e, "the change was not saved")));
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
        {workspace.shared && <NotifyModeField workspace={workspace} />}
        <p className="ws-link j-meta">
          Whether this device shows a notification is set on your{" "}
          <a href="/me">Profile page</a>.
        </p>
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

// The people of a shared workspace, named: the owner first, then the members.
// The roster names them (`/workspaces/options`), and the session names the
// person who signed in, whom that list leaves out. A person the viewer cannot
// name is shown by their id.
function People({ workspace }: { workspace: Workspace }) {
  const session = useContext(SessionContext);
  const [named, setNamed] = useState<Record<string, string>>({});

  useEffect(() => {
    let live = true;
    getOptions()
      .then((o) => live && setNamed(Object.fromEntries(o.people.map((p) => [p.id, p.name]))))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const names = [workspace.owner, ...workspace.members].map((id) =>
    id === session?.person.id ? session.person.name : (named[id] ?? id),
  );
  return <span className="subtitle ws-people-line">{names.join(", ")}</span>;
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
        <h1>
          {workspace.title}
          {workspace.shared && <SharedMark />}
          <WorkCharm charm={workspace.charm} />
        </h1>
        {workspace.shared && <People workspace={workspace} />}
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

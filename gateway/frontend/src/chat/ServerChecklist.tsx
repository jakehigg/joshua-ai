// A checklist of MCP servers, with the one line that says what each is for.

import type { ServerOption } from "../api/workspaces";

export function ServerChecklist({
  servers,
  picked,
  onChange,
}: {
  servers: ServerOption[];
  picked: string[];
  onChange: (picked: string[]) => void;
}) {
  const toggle = (name: string) =>
    onChange(picked.includes(name) ? picked.filter((n) => n !== name) : [...picked, name]);
  return (
    <fieldset className="ws-servers">
      <legend className="j-field-label">Servers</legend>
      {servers.length === 0 && <p className="j-meta">No servers to pick.</p>}
      {servers.map((s) => (
        <label key={s.name} className="ws-server">
          <input
            type="checkbox"
            checked={picked.includes(s.name)}
            onChange={() => toggle(s.name)}
          />
          <span>
            <strong>{s.name}</strong>
            {s.description !== s.name && <span className="j-meta"> {s.description}</span>}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

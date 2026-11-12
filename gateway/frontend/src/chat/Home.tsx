// The page at `/chat`: type a person id and open that chat at `/chat/<id>`.
// The last id is kept in localStorage and filled in.

import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import Shell from "../shell/Shell";
import { PERSON_ID } from "./people";

export const LAST_PERSON_KEY = "joshua-chat:last-person";

export function rememberPerson(person: string): void {
  try {
    window.localStorage.setItem(LAST_PERSON_KEY, person);
  } catch {
    // Storage is blocked; the field starts empty next time.
  }
}

function lastPerson(): string {
  try {
    return window.localStorage.getItem(LAST_PERSON_KEY) ?? "";
  } catch {
    return "";
  }
}

export function personProblem(id: string): string | null {
  if (!id) return null;
  if (!PERSON_ID.test(id)) {
    return "Use a lowercase letter first, then up to 31 lowercase letters, digits, or -.";
  }
  return null;
}

export function ChatHome() {
  const navigate = useNavigate();
  const [id, setId] = useState(lastPerson);
  const value = id.trim();
  const problem = personProblem(value);
  const ok = value !== "" && problem === null;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!ok) return;
    rememberPerson(value);
    navigate(`/chat/${encodeURIComponent(value)}`);
  };

  return (
    <Shell section="chat">
      <form className="chat-home" onSubmit={onSubmit}>
        <label className="j-field-label" htmlFor="person-id">
          Your id
        </label>
        <div className="chat-home-row">
          <label className="j-field">
            <input
              id="person-id"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="alex"
              autoCapitalize="none"
              autoCorrect="off"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={problem !== null || undefined}
              aria-describedby={problem ? "person-problem" : undefined}
            />
          </label>
          <button type="submit" className="j-btn primary" disabled={!ok}>
            Go
          </button>
        </div>
        {problem && (
          <p id="person-problem" className="j-error">
            {problem}
          </p>
        )}
      </form>
    </Shell>
  );
}

import { useState, type FormEvent } from "react";

import { ApiError, postJson } from "../../api/client";
import Chip from "../../ui/Chip";
import ErrorLine from "../../ui/ErrorLine";

/** The three roles of a person, in the order the segmented control shows them. */
export const ROLES = ["guest", "member", "owner"] as const;

export type Role = (typeof ROLES)[number];

/** The role a person gets when the owner changes nothing. */
export const DEFAULT_ROLE: Role = "member";

/** The answer of `POST /api/v1/admin/users`. */
export interface AddAnswer {
  secret_set?: boolean;
}

/**
 * The one dialog that adds a person: who they are, the id and the handle
 * they are known by, their role, and the secret they sign in with.
 *
 * It is one form and one call. `POST /api/v1/admin/users` adds the person
 * and sets the secret, so the owner never adds a person and then goes
 * looking for a second page to let them in. A blank secret is allowed: that
 * person cannot sign in yet, and the row of the table says so and offers
 * "Set secret".
 *
 * `onAdded` tells the table to read the roster again.
 */
export default function AddPersonDialog({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: () => void;
}) {
  const [name, setName] = useState<string>("");
  const [id, setId] = useState<string>("");
  const [handle, setHandle] = useState<string>("");
  const [role, setRole] = useState<Role>(DEFAULT_ROLE);
  const [secret, setSecret] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await postJson<AddAnswer>("/admin/users", {
        id: id.trim(),
        name: name.trim(),
        handle: handle.trim(),
        role,
        secret,
      });
      onAdded();
      onClose();
    } catch (exc: unknown) {
      setBusy(false);
      setError(exc instanceof ApiError ? exc.message : "that person was not added");
      // A refused secret is the one refusal that added the person all the
      // same, so the table reads the roster again and the row is there with
      // no secret on it.
      onAdded();
    }
  }

  return (
    <div className="v-admin-overlay">
      <form
        className="v-admin-dialog j-stack"
        role="dialog"
        aria-modal="true"
        aria-label="Add a person"
        onSubmit={submit}
      >
        <h2 className="j-label">add a person</h2>
        <label className="j-field-label">
          their name
          <span className="j-field">
            <input
              name="name"
              value={name}
              placeholder="Iris"
              required
              onChange={(event) => setName(event.target.value)}
            />
          </span>
        </label>
        <label className="j-field-label">
          their id
          <span className="j-field">
            <input
              name="id"
              value={id}
              placeholder="iris"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              required
              onChange={(event) => setId(event.target.value)}
            />
          </span>
        </label>
        <label className="j-field-label">
          their handle
          <span className="j-field">
            <input
              name="handle"
              value={handle}
              placeholder="telegram:iris"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              onChange={(event) => setHandle(event.target.value)}
            />
          </span>
        </label>
        <div className="j-field-label">
          their role
          <div className="j-chips" role="group" aria-label="their role">
            {ROLES.map((each) => (
              <Chip key={each} on={role === each} onClick={() => setRole(each)}>
                {each}
              </Chip>
            ))}
          </div>
        </div>
        <label className="j-field-label">
          the secret they sign in with
          <span className="j-field">
            <input
              name="secret"
              type="password"
              value={secret}
              autoComplete="new-password"
              onChange={(event) => setSecret(event.target.value)}
            />
          </span>
        </label>
        <p className="v-hint j-muted">
          With no secret that person is on the roster and cannot sign in. You can set one later.
        </p>
        {error !== "" && <ErrorLine text={error} />}
        <div className="j-row-flex">
          <button className="j-btn primary" type="submit" disabled={busy}>
            Add
          </button>
          <button className="j-btn ghost" type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

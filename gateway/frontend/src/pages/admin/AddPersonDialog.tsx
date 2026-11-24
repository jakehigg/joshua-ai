import { useEffect, useState, type FormEvent } from "react";

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
 * The rule for the id of a person, in words. The roster refuses an id that
 * breaks it with the pattern it checks, which says the same thing to nobody,
 * so the dialog says the rule and the owner can fix the field from it.
 */
export const ID_RULE =
  "An id holds lower-case letters, digits and hyphens, starts with a letter or a " +
  "digit, and is at most 32 characters long.";

/** The same rule, to keep an id the roster refuses from leaving the dialog. */
const ID_OK = /^[a-z0-9][a-z0-9-]{0,31}$/;

/**
 * What a refused add says. The roster answers the pattern of the id it
 * checks, and the dialog says the rule in words instead; every other refusal
 * is the sentence the roster sent.
 */
function reason(exc: unknown): string {
  if (!(exc instanceof ApiError)) {
    return "that person was not added";
  }
  return exc.message.includes("id must match") ? ID_RULE : exc.message;
}

/**
 * The one dialog that adds a person: who they are, the id, the handle
 * they are known by, their role, and the secret they sign in with. The
 * handle is optional: a person with none signs in to the viewer and reaches
 * Joshua from its chat or from the terminal.
 *
 * It is one form and one call. `POST /api/v1/admin/users` adds the person
 * and sets the secret, so the owner never adds a person and then goes
 * looking for a second page to let them in. A blank secret is allowed: that
 * person cannot sign in yet, and the row of the table says so and offers
 * "Set secret".
 *
 * An id that breaks the rule of the roster never leaves the dialog: the
 * field is checked here and the error is :data:`ID_RULE`, the rule in words,
 * and the same sentence stands in for the pattern when the roster is the one
 * that refuses. Escape closes the dialog, as it closes every other one.
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

  // Escape closes the dialog, as it closes the sheets of the app.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!ID_OK.test(id.trim())) {
      setError(ID_RULE);
      return;
    }
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
      setError(reason(exc));
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
          their handle (optional)
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

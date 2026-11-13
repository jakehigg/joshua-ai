import { Fragment, useEffect, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, getJson, postJson } from "../../api/client";
import Empty from "../../ui/Empty";
import ErrorLine from "../../ui/ErrorLine";
import Spinner from "../../ui/Spinner";
import AddPersonDialog from "./AddPersonDialog";

/** One person of `GET /api/v1/admin/users`. */
export interface AdminUser {
  id: string;
  name: string;
  role: string;
  has_secret: boolean;
}

/** The answer of `GET /api/v1/admin/users`. */
export interface AdminUsers {
  users: AdminUser[];
}

/** What the secret column says, with a secret and without one. */
export const SECRET_SET = "Set";
export const SECRET_NOT_SET = "Not set — can't sign in";

/** The two things a row opens under itself: the secret form, or the remove. */
type Open = { id: string; kind: "secret" | "remove" } | null;

/**
 * Where a role goes, and what the button that moves it says. A guest and a
 * member are promoted, one tier at a time; an owner is demoted to a member.
 * The owner who manages the roster is never on a row of their own making:
 * the route refuses a change that would leave the instance with no owner.
 */
export function roleStep(role: string): { label: string; next: string } {
  if (role === "owner") {
    return { label: "Demote", next: "member" };
  }
  return { label: "Promote", next: role === "guest" ? "member" : "owner" };
}

/** What a disabled Demote or Remove says on the row of the only owner. */
export const ONLY_OWNER = "the only owner: make another person the owner first";

/** True when `user` is the one owner of `users`. */
export function isOnlyOwner(user: AdminUser, users: AdminUser[]): boolean {
  return user.role === "owner" && users.filter((each) => each.role === "owner").length === 1;
}

/** The class of an action button, primary for the one the row leads with. */
function action(primary: boolean): string {
  return primary ? "j-btn sm primary" : "j-btn sm";
}

/**
 * The Users section: who is on the roster, who can sign in, and the four
 * writes of a row.
 *
 * The roster comes from `GET /api/v1/admin/users`: the people of
 * `joshua.yaml`, each with the role they hold and whether core has a secret
 * for them. A person with no secret is on the roster and cannot sign in, so
 * their row says so and leads with "Set secret" rather than with the role.
 *
 * Every write is one call of the owner's routes — the secret, the role, the
 * remove, and the add of the dialog — and each one is followed by a fresh
 * read of the roster, because core owns it and this page holds no copy.
 */
export default function Users() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string>("");
  const [open, setOpen] = useState<Open>(null);
  const [secret, setSecret] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [adding, setAdding] = useState<boolean>(false);
  // Bumped after every write: the roster is read again.
  const [tick, setTick] = useState<number>(0);

  useEffect(() => {
    let live = true;
    getJson<AdminUsers>("/admin/users")
      .then((answer) => {
        if (live) {
          setUsers(answer.users);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the roster did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [tick]);

  async function write(path: string, body: unknown): Promise<void> {
    setBusy(true);
    setError("");
    try {
      await postJson<Record<string, unknown>>(path, body);
      close();
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : "that did not work");
    }
    setBusy(false);
    setTick((count) => count + 1);
  }

  function close(): void {
    setOpen(null);
    setSecret("");
  }

  function saveSecret(event: FormEvent<HTMLFormElement>, id: string): void {
    event.preventDefault();
    void write(`/admin/users/${encodeURIComponent(id)}/secret`, { secret });
  }

  function setRole(user: AdminUser): void {
    void write(`/admin/users/${encodeURIComponent(user.id)}/role`, {
      role: roleStep(user.role).next,
    });
  }

  function remove(id: string): void {
    void write(`/admin/users/${encodeURIComponent(id)}/remove`, {});
  }

  /** The row under a row: the secret form, or the question of the remove. */
  function panel(user: AdminUser): ReactNode {
    if (open === null || open.id !== user.id) {
      return null;
    }
    if (open.kind === "secret") {
      return (
        <form className="v-user-form" onSubmit={(event) => saveSecret(event, user.id)}>
          <label className="j-field-label">
            the secret {user.name} signs in with
            <span className="j-field">
              <input
                name="secret"
                type="password"
                value={secret}
                autoComplete="new-password"
                required
                onChange={(event) => setSecret(event.target.value)}
              />
            </span>
          </label>
          <button className="j-btn sm primary" type="submit" disabled={busy}>
            Save
          </button>
          <button className="j-btn sm ghost" type="button" onClick={close}>
            Cancel
          </button>
        </form>
      );
    }
    return (
      <p className="v-user-form j-row-flex">
        Remove {user.name}? Their handles and their secret go, and what they wrote stays.
        <button className="j-btn sm danger" type="button" disabled={busy} onClick={() => remove(user.id)}>
          Remove
        </button>
        <button className="j-btn sm ghost" type="button" onClick={close}>
          Cancel
        </button>
      </p>
    );
  }

  function row(user: AdminUser): ReactNode {
    const step = roleStep(user.role);
    const onlyOwner = isOnlyOwner(user, users ?? []);
    const secretButton = (
      <button
        key="secret"
        type="button"
        className={action(!user.has_secret)}
        onClick={() => {
          setSecret("");
          setOpen({ id: user.id, kind: "secret" });
        }}
      >
        Set secret
      </button>
    );
    const roleButton = (
      <button
        key="role"
        type="button"
        className={action(false)}
        disabled={busy || onlyOwner}
        title={onlyOwner ? ONLY_OWNER : undefined}
        onClick={() => setRole(user)}
      >
        {step.label}
      </button>
    );
    const removeButton = (
      <button
        key="remove"
        type="button"
        className="j-btn sm danger"
        disabled={onlyOwner}
        title={onlyOwner ? ONLY_OWNER : undefined}
        onClick={() => setOpen({ id: user.id, kind: "remove" })}
      >
        Remove
      </button>
    );
    const under = panel(user);
    return (
      <Fragment key={user.id}>
        <tr>
          <td>
            <span className="j-list-main">
              <span className="j-list-title">{user.name}</span>
              <span className="j-list-sub">{user.id}</span>
            </span>
          </td>
          <td>
            <span className="j-badge plain">{user.role}</span>
          </td>
          <td className={user.has_secret ? "" : "v-user-nosecret"}>
            {user.has_secret ? SECRET_SET : SECRET_NOT_SET}
          </td>
          <td className="v-user-actions">
            {user.has_secret
              ? [roleButton, secretButton, removeButton]
              : [secretButton, roleButton, removeButton]}
          </td>
        </tr>
        {under !== null && (
          <tr>
            <td colSpan={4}>{under}</td>
          </tr>
        )}
      </Fragment>
    );
  }

  function table(): ReactNode {
    if (users === null) {
      return <Spinner label="Loading the people" />;
    }
    if (users.length === 0) {
      return <Empty text="nobody is on the roster yet." />;
    }
    return (
      <div className="j-table-wrap">
        <table className="j-table">
          <thead>
            <tr>
              <th>Person</th>
              <th>Role</th>
              <th>Secret</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>{users.map(row)}</tbody>
        </table>
      </div>
    );
  }

  return (
    <section className="j-stack">
      <div className="j-row-flex">
        <h2 className="j-label">people</h2>
        <span className="j-spacer" />
        <button className="j-btn primary sm" type="button" onClick={() => setAdding(true)}>
          Add a person
        </button>
      </div>
      {error !== "" && <ErrorLine text={error} />}
      {table()}
      {adding && (
        <AddPersonDialog
          onClose={() => setAdding(false)}
          onAdded={() => setTick((count) => count + 1)}
        />
      )}
    </section>
  );
}

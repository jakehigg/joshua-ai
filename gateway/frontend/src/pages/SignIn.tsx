import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, getJson, postJson, type Person } from "../api/client";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** The answer of `GET /api/v1/auth-method`: how this instance signs a person in. */
export interface AuthMethod {
  method: "anonymous" | "pin" | "password";
  anonymous: boolean;
}

/** The answer of `GET /api/v1/sign-in/people`, which only the pin method has. */
export interface SignInPeople {
  people: Person[];
}

/** The answer of `POST /api/v1/session/sign-in`. */
interface SignInAnswer {
  ok?: boolean;
}

/**
 * What the method calls the secret. It is the one word of the page the
 * method changes, so there is one form here and no second copy of it.
 */
export type SecretLabel = "PIN" | "password";

/** What a refused sign-in says, and it says nothing more than this. */
function refused(label: SecretLabel): string {
  return `That ${label} didn't match. Try again.`;
}

/**
 * The credential form of both methods: who the person is, their secret, and
 * whether this device remembers them.
 *
 * `label` is what the method calls the secret. With `onBack` the person came
 * from the list of a pin instance, so the id is already filled in and "Not
 * you?" goes back to the list; without it the person types the id, as the
 * password method asks them to.
 */
function CredentialForm({
  label,
  person,
  onPerson,
  onBack,
  secret,
  onSecret,
  remember,
  onRemember,
  error,
  busy,
  onSubmit,
}: {
  label: SecretLabel;
  person: string;
  onPerson: (value: string) => void;
  onBack: (() => void) | null;
  secret: string;
  onSecret: (value: string) => void;
  remember: boolean;
  onRemember: (value: boolean) => void;
  error: string;
  busy: boolean;
  onSubmit: () => void;
}) {
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onSubmit();
  }

  return (
    <form className="j-stack" onSubmit={submit}>
      <label className="j-field-label">
        your id
        <span className="j-field">
          <input
            name="person"
            value={person}
            placeholder="alex"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="username"
            spellCheck={false}
            readOnly={onBack !== null}
            required
            onChange={(event) => onPerson(event.target.value)}
          />
        </span>
      </label>
      {onBack !== null && (
        <p className="j-meta">
          <button type="button" className="j-btn ghost sm" onClick={onBack}>
            Not you?
          </button>
        </p>
      )}
      <label className="j-field-label">
        your {label}
        <span className="j-field">
          <input
            name="secret"
            type="password"
            value={secret}
            autoComplete="current-password"
            required
            onChange={(event) => onSecret(event.target.value)}
          />
        </span>
      </label>
      <label className="j-row-flex">
        <input
          type="checkbox"
          className="j-check"
          name="remember"
          checked={remember}
          onChange={(event) => onRemember(event.target.checked)}
        />
        Remember this device
      </label>
      {error !== "" && <ErrorLine text={error} />}
      <div className="j-row-flex">
        <button className="j-btn primary" type="submit" disabled={busy}>
          Sign in
        </button>
      </div>
    </form>
  );
}

/**
 * `/sign-in`: the one page of the viewer that needs no session.
 *
 * `GET /api/v1/auth-method` says what to draw. The `password` method draws
 * the credential form at once, because the person types their own id. The
 * `pin` method draws the people of `GET /api/v1/sign-in/people` first, and a
 * tap on a name opens the same form with the id filled in. The `anonymous`
 * method asks for nothing at all: the app never sends anybody here, because
 * every request of such an instance is served as the anonymous identity.
 *
 * A sign-in that works carries the session cookie, so the page goes to the
 * wiki. One that does not says only that the secret did not match: the
 * answer of the viewer names neither the person nor the field, and neither
 * does this page.
 */
export default function SignIn() {
  const navigate = useNavigate();
  const [auth, setAuth] = useState<AuthMethod | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [failed, setFailed] = useState<string>("");
  const [person, setPerson] = useState<string>("");
  // True once a name of the list is tapped: the pin method then shows the
  // same form as the password method.
  const [picked, setPicked] = useState<boolean>(false);
  const [secret, setSecret] = useState<string>("");
  const [remember, setRemember] = useState<boolean>(false);
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    let live = true;
    document.title = "sign in";
    getJson<AuthMethod>("/auth-method")
      .then(async (answer) => {
        if (!live) {
          return;
        }
        setAuth(answer);
        if (answer.method !== "pin") {
          return;
        }
        const list = await getJson<SignInPeople>("/sign-in/people");
        if (live) {
          setPeople(list.people);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setFailed(exc instanceof ApiError ? exc.message : "the sign-in did not load");
        }
      });
    return () => {
      live = false;
    };
  }, []);

  const label: SecretLabel = auth?.method === "password" ? "password" : "PIN";

  async function submit(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const answer = await postJson<SignInAnswer>("/session/sign-in", {
        person: person.trim(),
        secret,
        remember,
      });
      if (answer.ok === true) {
        navigate("/");
        return;
      }
      setBusy(false);
      setError(refused(label));
    } catch (exc: unknown) {
      setBusy(false);
      // A refusal is the one line, whatever the viewer said about it. Any
      // other answer — no core behind the viewer, a cross-site origin — says
      // what the viewer said, because the secret is not what was wrong.
      setError(exc instanceof ApiError && exc.status !== 401 ? exc.message : refused(label));
    }
  }

  function back(): void {
    setPicked(false);
    setPerson("");
    setSecret("");
    setError("");
  }

  function pick(id: string): void {
    setPerson(id);
    setPicked(true);
    setSecret("");
    setError("");
  }

  function body() {
    if (failed !== "") {
      return <ErrorLine text={failed} />;
    }
    if (auth === null) {
      return <Spinner label="Loading the sign-in" />;
    }
    if (auth.method === "anonymous") {
      return (
        <p className="j-meta">
          This Joshua asks nobody to sign in. <a href="/">Go to the wiki</a>.
        </p>
      );
    }
    if (auth.method === "password" || picked) {
      return (
        <CredentialForm
          label={label}
          person={person}
          onPerson={setPerson}
          onBack={auth.method === "pin" ? back : null}
          secret={secret}
          onSecret={setSecret}
          remember={remember}
          onRemember={setRemember}
          error={error}
          busy={busy}
          onSubmit={submit}
        />
      );
    }
    return (
      <div className="j-stack">
        <p className="j-meta">Who are you?</p>
        {people.length === 0 ? (
          <p className="j-empty">nobody can sign in yet.</p>
        ) : (
          people.map((one) => (
            <button
              key={one.id}
              type="button"
              className="j-btn block"
              onClick={() => pick(one.id)}
            >
              {one.name}
            </button>
          ))
        )}
      </div>
    );
  }

  return (
    <div className="j-shell">
      <div className="j-main">
        <main className="j-content">
          <Page title="Sign in">{body()}</Page>
        </main>
      </div>
    </div>
  );
}

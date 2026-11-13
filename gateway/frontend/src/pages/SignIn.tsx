import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, getJson, postJson, type Person } from "../api/client";
import ErrorLine from "../ui/ErrorLine";
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

/** A person tapped on the list, with the color of their tile. */
interface Picked {
  person: Person;
  tone: number;
}

/** The initials of a name, for the round badge of a person. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter((part) => part !== "");
  const first = parts[0] ?? "?";
  const last = parts[parts.length - 1] ?? "";
  const letters = parts.length > 1 ? first.charAt(0) + last.charAt(0) : first.charAt(0);
  return letters.toUpperCase();
}

/**
 * The round badge of a person: their initials on one of five colors. The
 * color is the place of the person on the list, so the first five people
 * never share one, and the greeting of the form keeps the color of the tile.
 */
function Avatar({ person, tone, large = false }: { person: Person; tone: number; large?: boolean }) {
  return (
    <span className={`signin-avatar h${tone % 5}${large ? " lg" : ""}`} aria-hidden="true">
      {initials(person.name)}
    </span>
  );
}

/** The top of the card: a badge or the mark of Joshua, a title, and one line. */
function Head({ title, sub, person }: { title: string; sub?: string; person?: Picked }) {
  return (
    <header className="signin-head">
      {person !== undefined ? (
        <Avatar person={person.person} tone={person.tone} large />
      ) : (
        <img className="signin-mark" src="/logo-64.png" alt="" width={48} height={48} />
      )}
      <h1>{title}</h1>
      {sub !== undefined && <p>{sub}</p>}
    </header>
  );
}

/**
 * The credential form of both methods: who the person is, their secret, and
 * whether this device remembers them.
 *
 * `label` is what the method calls the secret. With `onBack` the person came
 * from the list of a pin instance, so the id is already filled in and "Not
 * you?" goes back to the list; without it the person types the id, as the
 * password method asks them to. The id stays editable in both cases: a
 * password manager can write its own username into the field, and the person
 * must be able to correct it.
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
    <form className="signin-form" onSubmit={submit}>
      <div className="signin-field">
        <div className="signin-label-row">
          <label className="signin-label" htmlFor="signin-person">
            Person
          </label>
          {onBack !== null && (
            <button type="button" className="signin-link" onClick={onBack}>
              Not you?
            </button>
          )}
        </div>
        <span className="j-field">
          <input
            id="signin-person"
            name="person"
            value={person}
            placeholder="your id"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="username"
            spellCheck={false}
            required
            onChange={(event) => onPerson(event.target.value)}
          />
        </span>
      </div>
      <label className="signin-field">
        <span className="signin-label">{label === "PIN" ? "PIN" : "Password"}</span>
        <span className="j-field">
          <input
            name="secret"
            type="password"
            value={secret}
            autoComplete="current-password"
            autoFocus={onBack !== null}
            required
            onChange={(event) => onSecret(event.target.value)}
          />
        </span>
      </label>
      {error !== "" && <ErrorLine text={error} />}
      <label className="signin-remember">
        <input
          type="checkbox"
          className="j-check"
          name="remember"
          checked={remember}
          onChange={(event) => onRemember(event.target.checked)}
        />
        Remember this device
      </label>
      <button className="j-btn primary signin-submit" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

/**
 * What a pin instance shows while no person has a secret. The list holds only
 * the people who have one, so an empty list means that the secrets are not
 * set yet. The page says so, and says how the first one is set.
 */
function NoSecrets({ label }: { label: SecretLabel }) {
  return (
    <div className="signin-empty">
      <p className="signin-empty-lead">Nobody can sign in yet, because nobody has a {label}.</p>
      <p>Set a {label} for the owner first. Run this command in the core container:</p>
      <pre>
        <code>{`python -m joshua_core people auth-reset --id <owner id> --secret '<${label}>'`}</code>
      </pre>
      <p>Then the owner signs in and sets a {label} for each other person under Admin → Users.</p>
    </div>
  );
}

/**
 * `/sign-in`: the one page of the viewer that needs no session.
 *
 * `GET /api/v1/auth-method` says what to draw. The `password` method draws
 * the credential form at once, because the person types their own id. The
 * `pin` method draws the people of `GET /api/v1/sign-in/people` first, and a
 * tap on a name opens the same form with the id filled in. The person can
 * still change the id. The `anonymous`
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
  // The person whose name was tapped on the list: the pin method then shows
  // the same form as the password method, with a greeting of that person.
  const [picked, setPicked] = useState<Picked | null>(null);
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
    setPicked(null);
    setPerson("");
    setSecret("");
    setError("");
  }

  function pick(one: Person, tone: number): void {
    setPerson(one.id);
    setPicked({ person: one, tone });
    setSecret("");
    setError("");
  }

  function body() {
    if (failed !== "") {
      return (
        <>
          <Head title="Sign in to Joshua" />
          <ErrorLine text={failed} />
        </>
      );
    }
    if (auth === null) {
      return <Spinner label="Loading the sign-in" />;
    }
    if (auth.method === "anonymous") {
      return (
        <>
          <Head title="Joshua" />
          <p className="signin-note">
            This Joshua asks nobody to sign in. <a href="/">Go to the wiki</a>.
          </p>
        </>
      );
    }
    const form = (
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
    if (auth.method === "password") {
      return (
        <>
          <Head title="Sign in to Joshua" sub="Your person id and your password." />
          {form}
        </>
      );
    }
    if (picked !== null) {
      return (
        <>
          <Head title={`Hi, ${picked.person.name}`} sub="Enter your PIN." person={picked} />
          {form}
        </>
      );
    }
    if (people.length === 0) {
      return (
        <>
          <Head title="Sign in to Joshua" />
          <NoSecrets label={label} />
        </>
      );
    }
    return (
      <>
        <Head title="Who is signing in?" sub="Pick your name, then enter your PIN." />
        <div className="signin-people">
          {people.map((one, index) => (
            <button key={one.id} type="button" className="signin-person" onClick={() => pick(one, index)}>
              <Avatar person={one} tone={index} />
              <span className="signin-person-name">{one.name}</span>
              <span className="signin-person-role">{one.role}</span>
            </button>
          ))}
        </div>
      </>
    );
  }

  return (
    <main className="signin-screen">
      <section className="signin-card">{body()}</section>
    </main>
  );
}

import { useEffect, useState, type FormEvent } from "react";

import { ApiError, getJson, postJson } from "../api/client";
import ErrorLine from "../ui/ErrorLine";
import type { AuthMethod } from "./SignIn";

/** The answer of `GET /api/v1/profile`: what a person may change about themselves. */
export interface Profile {
  prompt: string | null;
  journal: JournalConsent;
  /** True when `joshua.yaml` also names the person. A save then overrides those two fields only. */
  in_config: boolean;
}

export type JournalConsent = "auto" | "ask" | "off";

/** The three answers of the journal consent, as the page words them. */
export const JOURNAL_CHOICES: { value: JournalConsent; label: string; hint: string }[] = [
  { value: "auto", label: "Automatic", hint: "Joshua writes journal entries about you on its own." },
  { value: "ask", label: "Ask first", hint: "Joshua asks you before it writes an entry about you." },
  { value: "off", label: "Off", hint: "Joshua writes an entry about you only when you ask." },
];

/** The longest prompt core keeps. */
export const PROMPT_MAX = 4000;

function message(exc: unknown, fallback: string): string {
  return exc instanceof ApiError ? exc.message : fallback;
}

/**
 * The card that changes the secret of the person who signed in, at
 * `POST /api/v1/session/secret`. The current secret goes with the new one.
 * The change signs out every other browser of the person, and this one stays
 * signed in.
 */
function SecretCard() {
  const [label, setLabel] = useState<"PIN" | "password">("PIN");
  const [shown, setShown] = useState<boolean>(true);
  const [current, setCurrent] = useState<string>("");
  const [next, setNext] = useState<string>("");
  const [again, setAgain] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [done, setDone] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    let live = true;
    getJson<AuthMethod>("/auth-method")
      .then((answer) => {
        if (!live) {
          return;
        }
        setLabel(answer.method === "password" ? "password" : "PIN");
        // An instance with no sign-in has no secret to change.
        setShown(answer.method !== "anonymous");
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError("");
    setDone(false);
    if (next !== again) {
      setError(`The new ${label} and its repeat are not the same.`);
      return;
    }
    setBusy(true);
    try {
      await postJson("/session/secret", { secret: current, new_secret: next });
      setCurrent("");
      setNext("");
      setAgain("");
      setDone(true);
    } catch (exc: unknown) {
      setError(message(exc, `The ${label} did not change.`));
    } finally {
      setBusy(false);
    }
  }

  if (!shown) {
    return null;
  }
  return (
    <section className="j-card profile-card" aria-labelledby="profile-secret">
      <h2 id="profile-secret" className="profile-card-title">
        Your {label}
      </h2>
      <p className="j-meta">A change signs you out of every other browser. This one stays signed in.</p>
      <form className="profile-form" onSubmit={submit}>
        <label className="profile-field">
          <span className="profile-label">Current {label}</span>
          <span className="j-field">
            <input
              name="secret"
              type="password"
              autoComplete="current-password"
              value={current}
              required
              onChange={(event) => setCurrent(event.target.value)}
            />
          </span>
        </label>
        <label className="profile-field">
          <span className="profile-label">New {label}</span>
          <span className="j-field">
            <input
              name="new_secret"
              type="password"
              autoComplete="new-password"
              value={next}
              required
              onChange={(event) => setNext(event.target.value)}
            />
          </span>
        </label>
        <label className="profile-field">
          <span className="profile-label">New {label} again</span>
          <span className="j-field">
            <input
              name="new_secret_again"
              type="password"
              autoComplete="new-password"
              value={again}
              required
              onChange={(event) => setAgain(event.target.value)}
            />
          </span>
        </label>
        {error !== "" && <ErrorLine text={error} />}
        {done && <p className="profile-ok">Your {label} is changed.</p>}
        <div>
          <button className="j-btn primary" type="submit" disabled={busy}>
            Change {label}
          </button>
        </div>
      </form>
    </section>
  );
}

/**
 * The card of what Joshua knows about how to treat the person: their own
 * prompt and their journal consent, at `GET` and `POST /api/v1/profile`.
 * Core keeps the instructions in a file of the person's own and writes the
 * two settings to the people file, which overlays those fields on
 * `joshua.yaml`. Joshua reads the change on the next turn.
 */
function JoshuaCard() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [prompt, setPrompt] = useState<string>("");
  const [journal, setJournal] = useState<JournalConsent>("auto");
  const [failed, setFailed] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [done, setDone] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    let live = true;
    getJson<Profile>("/profile")
      .then((answer) => {
        if (!live) {
          return;
        }
        setProfile(answer);
        setPrompt(answer.prompt ?? "");
        setJournal(answer.journal);
      })
      .catch((exc: unknown) => {
        if (live) {
          setFailed(message(exc, "Your settings did not load."));
        }
      });
    return () => {
      live = false;
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError("");
    setDone(false);
    setBusy(true);
    try {
      const answer = await postJson<Profile>("/profile", { prompt: prompt.trim(), journal });
      setProfile(answer);
      setPrompt(answer.prompt ?? "");
      setJournal(answer.journal);
      setDone(true);
    } catch (exc: unknown) {
      setError(message(exc, "Your settings did not save."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="j-card profile-card" aria-labelledby="profile-joshua">
      <h2 id="profile-joshua" className="profile-card-title">
        Joshua and you
      </h2>
      {failed !== "" ? (
        <ErrorLine text={failed} />
      ) : profile === null ? (
        <p className="j-meta">Loading your settings…</p>
      ) : (
        <form className="profile-form" onSubmit={submit}>
          <label className="profile-field">
            <span className="profile-label">Your instructions to Joshua</span>
            <span className="profile-hint">
              Joshua reads these in every conversation with you. For example: how to talk to you,
              or what to keep in mind.
            </span>
            <span className="j-field">
              <textarea
                name="prompt"
                rows={5}
                maxLength={PROMPT_MAX}
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
              />
            </span>
          </label>
          <fieldset className="profile-field profile-choices">
            <legend className="profile-label">Journal entries about you</legend>
            {JOURNAL_CHOICES.map((choice) => (
              <label key={choice.value} className="profile-choice">
                <input
                  type="radio"
                  name="journal"
                  value={choice.value}
                  checked={journal === choice.value}
                  onChange={() => setJournal(choice.value)}
                />
                <span>
                  <span className="profile-choice-label">{choice.label}</span>
                  <span className="profile-hint">{choice.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {profile.in_config && (
            <p className="profile-note">
              The config file of this Joshua also names you. A save here overrides your instructions
              and your journal setting from that file. Your name and handles stay as the file sets
              them.
            </p>
          )}
          {error !== "" && <ErrorLine text={error} />}
          {done && <p className="profile-ok">Saved.</p>}
          <div>
            <button className="j-btn primary" type="submit" disabled={busy}>
              Save
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

/** The settings of the person who signed in, on their Profile page. */
export default function ProfileSettings() {
  return (
    <div className="profile-cards">
      <SecretCard />
      <JoshuaCard />
    </div>
  );
}

import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, getJson, postJson } from "../../api/client";
import Empty from "../../ui/Empty";
import ErrorLine from "../../ui/ErrorLine";
import Spinner from "../../ui/Spinner";

/** What kind of value a setting holds, as core's `kind` says: a line of text,
 * a switch, or a secret this page never reads back. */
export type SettingKind = "text" | "bool" | "secret";

/** One setting of `GET /api/v1/admin/settings`. */
export interface AdminSetting {
  key: string;
  label: string;
  hint: string;
  /** Which control the field gets. */
  kind: SettingKind;
  is_secret: boolean;
  /** Whether a value is stored. The only thing a secret ever says. */
  is_set: boolean;
  /** The stored text of a plain setting. A secret is always null. */
  value: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

/** One group of settings: one section of this page. */
export interface AdminSettingGroup {
  prefix: string;
  label: string;
  settings: AdminSetting[];
}

/** The answer of `GET /api/v1/admin/settings`. */
export interface AdminSettings {
  groups: AdminSettingGroup[];
}

/** What a secret field says, with a value stored and without one. */
export const SECRET_SET = "set";
export const SECRET_NOT_SET = "not set";

/** The two values a switch writes. Nobody types them. */
export const ON_VALUE = "true";
export const OFF_VALUE = "false";
/** Every value core reads as on (`is_true` in core's push module). */
const TRUE_WORDS = ["true", "yes", "on", "1"];

/** Whether a switch is on. */
export function isOn(value: string | null | undefined): boolean {
  return TRUE_WORDS.includes((value ?? "").trim().toLowerCase());
}

/** The keys of the push group this page says something of its own about. */
const PUSH_PREFIX = "push";
const PUBLIC_KEY = "push.vapid_public_key";
const PRIVATE_KEY = "push.vapid_private_key";
const SUBJECT_KEY = "push.vapid_subject";
const ENABLED_KEY = "push.enabled";

/** Where the docs are in the viewer: core ships them into the wiki. */
const NOTIFICATIONS_DOC = "/wiki/joshua-docs/notifications.md";

/** The placeholder of a field that has one, by key. */
const PLACEHOLDERS: Record<string, string> = { [SUBJECT_KEY]: "mailto:you@example.com" };

/** What a VAPID subject may be (RFC 8292). */
const SUBJECT_PREFIXES = ["mailto:", "https://"];

/**
 * Why this value is refused for this setting, or `""` when it is good.
 *
 * The server checks the same thing and is the authority; this one saves the
 * owner a round trip, and says the same sentence.
 */
export function refusalOf(setting: AdminSetting, value: string): string {
  if (setting.key !== SUBJECT_KEY) {
    return "";
  }
  const text = value.trim();
  if (SUBJECT_PREFIXES.some((prefix) => text.startsWith(prefix))) {
    return "";
  }
  return (
    `${SUBJECT_KEY} must start with mailto: or https://, as in ` +
    "mailto:you@example.com: a push service writes to it about this instance"
  );
}

/**
 * The one line the push group shows: which of the three keys are in, and
 * whether push is on.
 *
 * All four settings are needed, so a group with a key missing says what is
 * still to do, and the switch alone sends nothing.
 */
export function pushStatus(settings: AdminSetting[]): string {
  const stored = new Map(settings.map((setting) => [setting.key, setting]));
  const named: [string, string][] = [
    [PUBLIC_KEY, "the public key"],
    [PRIVATE_KEY, "the private key"],
    [SUBJECT_KEY, "the subject"],
  ];
  const missing = named
    .filter(([key]) => !(stored.get(key)?.is_set ?? false))
    .map(([, name]) => name);
  const on = isOn(stored.get(ENABLED_KEY)?.value);
  if (missing.length > 0) {
    const what = `${missing.join(" and ")} ${missing.length === 1 ? "is" : "are"} not set`;
    return on ? `Push is on, and sends nothing while ${what}.` : `Push is off: ${what}.`;
  }
  return on
    ? "The three keys are set and push is on. Each person turns their own devices on."
    : "The three keys are set. Turn push on below.";
}

/**
 * What the button of a secret says.
 *
 * With no value stored it is the step the owner has not taken yet, so it is
 * the primary button and names the thing in plain words: the label names the
 * standard ("VAPID private key") and the button leaves the acronym out.
 */
export function setLabel(setting: AdminSetting): string {
  if (setting.is_set) {
    return "Replace";
  }
  const plain = setting.label
    .split(/\s+/)
    .filter((word) => !/^[A-Z0-9]{2,}$/.test(word))
    .join(" ")
    .toLowerCase();
  return plain === "" ? "Set" : `Set the ${plain}`;
}

/**
 * The intro of a group: the steps the owner takes, in order, and where the
 * whole story is. A group with no intro here renders none.
 */
function intro(prefix: string): ReactNode {
  if (prefix !== PUSH_PREFIX) {
    return null;
  }
  return (
    <>
      <ol className="j-list-sub">
        <li>Make a VAPID pair with the one command in the notifications page of the docs.</li>
        <li>Paste the public key, then set the private key.</li>
        <li>Write the subject a push service writes to about this instance.</li>
        <li>
          Turn push on. Each person then turns on each device of theirs, on their Profile page.
        </li>
      </ol>
      <p className="j-list-sub">
        <a href={NOTIFICATIONS_DOC}>notifications.md</a> has that command, and the rest.
      </p>
    </>
  );
}

/** The drafts a fresh read starts from: the text of a plain setting, nothing
 * for a secret, because no stored secret ever reaches this page. */
export function draftsOf(groups: AdminSettingGroup[]): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const group of groups) {
    for (const setting of group.settings) {
      drafts[setting.key] = setting.is_secret ? "" : (setting.value ?? "");
    }
  }
  return drafts;
}

/** When a setting last changed, and who wrote it, as one line. */
export function changedLine(setting: AdminSetting): string {
  if (setting.updated_at === null) {
    return "";
  }
  const at = new Date(setting.updated_at);
  const when = Number.isNaN(at.getTime()) ? setting.updated_at : at.toLocaleString();
  return setting.updated_by ? `changed ${when} by ${setting.updated_by}` : `changed ${when}`;
}

/**
 * The Instance section: the settings of the instance, as the owner writes them.
 *
 * Core keeps a setting in its database, one row per key, so a restart keeps
 * it. `GET /api/v1/admin/settings` gives the groups, and each group is a
 * section here with a label, an intro that says what to do, a field, and a
 * save of its own. A later feature adds a group on core's side, and this page
 * renders it with no change.
 *
 * The `kind` of a field says which control it gets: a line of text, a
 * checkbox that writes `true` or `false`, or a secret. Nobody types the word
 * "true".
 *
 * A secret setting never comes back: its field says "set" or "not set" and
 * offers a replace, and what the owner types goes one way, to
 * `POST /api/v1/admin/settings`. A write of a secret needs
 * `JOSHUA_SETTINGS_KEY` on the Joshua side; without it core refuses that one
 * write, and the line of the refusal names the variable.
 */
export default function Instance() {
  const [groups, setGroups] = useState<AdminSettingGroup[] | null>(null);
  const [error, setError] = useState<string>("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // The key of the secret the owner is replacing, or "" for none.
  const [replacing, setReplacing] = useState<string>("");
  // The key of the last setting written, for the line that says so.
  const [saved, setSaved] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  // Bumped after every write: the settings are read again.
  const [tick, setTick] = useState<number>(0);

  useEffect(() => {
    let live = true;
    getJson<AdminSettings>("/admin/settings")
      .then((answer) => {
        if (live) {
          setGroups(answer.groups);
          setDrafts(draftsOf(answer.groups));
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the settings did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [tick]);

  /** One write of one setting, and the read that follows it. */
  async function write(setting: AdminSetting, value: string): Promise<void> {
    setBusy(true);
    setError("");
    setSaved("");
    try {
      await postJson<Record<string, unknown>>("/admin/settings", { key: setting.key, value });
      setSaved(setting.key);
      setReplacing("");
      setTick((count) => count + 1);
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : "that did not save");
    }
    setBusy(false);
  }

  async function save(event: FormEvent<HTMLFormElement>, setting: AdminSetting): Promise<void> {
    event.preventDefault();
    const value = drafts[setting.key] ?? "";
    const refusal = refusalOf(setting, value);
    if (refusal !== "") {
      setError(refusal);
      setSaved("");
      return;
    }
    await write(setting, value);
  }

  /** A switch: one tap writes `true` or `false`, so there is no Save. */
  function toggle(setting: AdminSetting): ReactNode {
    const changed = changedLine(setting);
    return (
      <div className="v-setting" key={setting.key}>
        <label className="j-row-flex">
          <input
            name={setting.key}
            type="checkbox"
            checked={isOn(drafts[setting.key])}
            disabled={busy}
            onChange={(event) => {
              const next = event.target.checked ? ON_VALUE : OFF_VALUE;
              setDrafts({ ...drafts, [setting.key]: next });
              void write(setting, next);
            }}
          />
          <span>{setting.label}</span>
        </label>
        {setting.hint !== "" && <p className="j-list-sub">{setting.hint}</p>}
        <span className="j-row-flex">
          {saved === setting.key && <span className="j-chip sm">saved</span>}
          {changed !== "" && <span className="j-list-sub">{changed}</span>}
        </span>
      </div>
    );
  }

  /** One setting: its label, its field, and its save. */
  function field(setting: AdminSetting): ReactNode {
    if (setting.kind === "bool") {
      return toggle(setting);
    }
    // A plain setting is always editable. A secret gets a field only while it
    // is being replaced, so the page never shows a box that looks like it
    // holds the stored one.
    const open = !setting.is_secret || replacing === setting.key;
    const changed = changedLine(setting);
    return (
      <form className="v-setting" key={setting.key} onSubmit={(event) => void save(event, setting)}>
        <label className="j-field-label">
          <span className="j-row-flex">
            {setting.label}
            {setting.is_secret && (
              <span className="j-badge plain">
                {setting.is_set ? SECRET_SET : SECRET_NOT_SET}
              </span>
            )}
          </span>
          {open && (
            <span className="j-field">
              <input
                name={setting.key}
                type={setting.is_secret ? "password" : "text"}
                value={drafts[setting.key] ?? ""}
                placeholder={PLACEHOLDERS[setting.key] ?? ""}
                autoComplete={setting.is_secret ? "new-password" : "off"}
                required={setting.is_secret}
                onChange={(event) =>
                  setDrafts({ ...drafts, [setting.key]: event.target.value })
                }
              />
            </span>
          )}
        </label>
        {setting.hint !== "" && <p className="j-list-sub">{setting.hint}</p>}
        <span className="j-row-flex">
          {open ? (
            <button className="j-btn sm primary" type="submit" disabled={busy}>
              Save
            </button>
          ) : (
            <button
              className={setting.is_set ? "j-btn sm" : "j-btn sm primary"}
              type="button"
              onClick={() => {
                setDrafts({ ...drafts, [setting.key]: "" });
                setReplacing(setting.key);
              }}
            >
              {setLabel(setting)}
            </button>
          )}
          {open && setting.is_secret && (
            <button className="j-btn sm ghost" type="button" onClick={() => setReplacing("")}>
              Cancel
            </button>
          )}
          {saved === setting.key && <span className="j-chip sm">saved</span>}
          {changed !== "" && <span className="j-list-sub">{changed}</span>}
        </span>
      </form>
    );
  }

  function sections(): ReactNode {
    if (groups === null) {
      return <Spinner label="Loading the settings" />;
    }
    if (groups.length === 0) {
      return <Empty text="this instance has no settings to edit yet." />;
    }
    return groups.map((group) => (
      <section className="j-stack" key={group.prefix}>
        <h2 className="j-label">{group.label}</h2>
        {intro(group.prefix)}
        {group.prefix === PUSH_PREFIX && <p className="j-meta">{pushStatus(group.settings)}</p>}
        {group.settings.map(field)}
      </section>
    ));
  }

  return (
    <section className="j-stack">
      {error !== "" && <ErrorLine text={error} />}
      {sections()}
    </section>
  );
}

import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, getJson, postJson } from "../../api/client";
import Empty from "../../ui/Empty";
import ErrorLine from "../../ui/ErrorLine";
import Spinner from "../../ui/Spinner";

/** One setting of `GET /api/v1/admin/settings`. */
export interface AdminSetting {
  key: string;
  label: string;
  hint: string;
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
 * section here with a label, a field, and a save of its own. A later feature
 * adds a group on core's side, and this page renders it with no change.
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

  async function save(event: FormEvent<HTMLFormElement>, setting: AdminSetting): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSaved("");
    try {
      await postJson<Record<string, unknown>>("/admin/settings", {
        key: setting.key,
        value: drafts[setting.key] ?? "",
      });
      setSaved(setting.key);
      setReplacing("");
      setTick((count) => count + 1);
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : "that did not save");
    }
    setBusy(false);
  }

  /** One setting: its label, its field, and its save. */
  function field(setting: AdminSetting): ReactNode {
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
              className="j-btn sm"
              type="button"
              onClick={() => {
                setDrafts({ ...drafts, [setting.key]: "" });
                setReplacing(setting.key);
              }}
            >
              {setting.is_set ? "Replace" : "Set"}
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

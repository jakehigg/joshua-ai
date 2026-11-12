/**
 * `/links`: the links of the sidebar, and the editor of a member.
 *
 * The page reads `GET /api/v1/links`, which carries the list, the names of
 * the built-in icons, and the token of the write. A member gets the editor
 * under the list: one row per link, an add row, a remove button and the two
 * buttons that move a row, because the order of the rows is the order of the
 * sidebar. Save posts the whole list to `POST /api/v1/links`, which replaces
 * the file, and a row the viewer refuses shows its message in place and
 * changes nothing.
 *
 * A guest sees the list and no editor.
 */

import { useEffect, useState } from "react";

import { ApiError, getJson, postJson } from "../api/client";
import Icon from "../shell/Icon";
import type { LinkRow, LinksData } from "../shell/LinksNav";
import Shell from "../shell/Shell";
import { DEFAULT_LINK_ICON, LINKS_SECTION, linkIcon } from "../shell/sections";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** The one line the page says about the links. */
export const ABOUT_TEXT =
  "Links to the tools you use beside Joshua, such as its addons. " +
  "Each one opens in a new tab.";

/** What the page says with no link yet. */
export const EMPTY_TEXT = "No links yet.";

/** The caps of the two fields, as `viewer_links` holds them. */
const MAX_TITLE = 40;
const MAX_URL = 500;

/** The answer of a write: the list as the file now holds it. */
export interface SaveAnswer {
  ok: boolean;
  links: LinkRow[];
}

/** The saved list, each link in a new tab. */
function List({ links }: { links: LinkRow[] }) {
  if (links.length === 0) {
    return <Empty text={EMPTY_TEXT} />;
  }
  return (
    <ul className="j-list v-link-list">
      {links.map((link, index) => (
        <li key={`${index}-${link.url}`}>
          <a className="j-list-row" href={link.url} target="_blank" rel="noopener noreferrer">
            <Icon paths={linkIcon(link.icon)} />
            <span className="j-list-main">
              <span className="j-list-title">{link.title}</span>
              <span className="j-list-sub">{link.url}</span>
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

/** The editor of a member: the rows, and the one Save of the whole list. */
function Editor({
  data,
  onSaved,
}: {
  data: LinksData;
  onSaved: (links: LinkRow[]) => void;
}) {
  const [rows, setRows] = useState<LinkRow[]>(data.links);
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  function change(index: number, patch: Partial<LinkRow>): void {
    setRows(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)));
  }

  function add(): void {
    setRows([...rows, { title: "", url: "", icon: DEFAULT_LINK_ICON }]);
  }

  function remove(index: number): void {
    setRows(rows.filter((_, at) => at !== index));
  }

  function move(index: number, to: number): void {
    if (to < 0 || to >= rows.length) {
      return;
    }
    const moved = [...rows];
    const row = moved[index];
    const other = moved[to];
    if (row === undefined || other === undefined) {
      return;
    }
    moved[index] = other;
    moved[to] = row;
    setRows(moved);
  }

  async function save(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const answer = await postJson<SaveAnswer>("/links", { csrf: data.csrf, links: rows });
      setRows(answer.links);
      onSaved(answer.links);
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : "the links were not saved");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2 className="j-label">Edit links</h2>
      <form
        className="v-links-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {error !== "" && <ErrorLine text={error} />}
        {rows.map((row, index) => (
          <fieldset className="v-link-row" key={index}>
            <legend className="j-label">{`Link ${index + 1}`}</legend>
            <label className="j-field">
              <input
                value={row.title}
                maxLength={MAX_TITLE}
                placeholder="Title"
                aria-label={`Title for row ${index + 1}`}
                onChange={(event) => change(index, { title: event.target.value })}
              />
            </label>
            <label className="j-field">
              <input
                type="url"
                value={row.url}
                maxLength={MAX_URL}
                placeholder="https://"
                aria-label={`URL for row ${index + 1}`}
                onChange={(event) => change(index, { url: event.target.value })}
              />
            </label>
            <label className="j-field v-icon-field">
              <Icon paths={linkIcon(row.icon)} />
              <select
                value={row.icon}
                aria-label={`Icon for row ${index + 1}`}
                onChange={(event) => change(index, { icon: event.target.value })}
              >
                {data.icons.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <div className="v-link-actions">
              <button
                className="j-btn sm ghost"
                type="button"
                aria-label={`Move row ${index + 1} up`}
                disabled={index === 0}
                onClick={() => move(index, index - 1)}
              >
                ↑
              </button>
              <button
                className="j-btn sm ghost"
                type="button"
                aria-label={`Move row ${index + 1} down`}
                disabled={index === rows.length - 1}
                onClick={() => move(index, index + 1)}
              >
                ↓
              </button>
              <button
                className="j-btn sm danger"
                type="button"
                aria-label={`Remove row ${index + 1}`}
                onClick={() => remove(index)}
              >
                remove
              </button>
            </div>
          </fieldset>
        ))}
        <p className="j-muted v-hint">
          Links open in a new tab. A URL must start with http:// or https://.
        </p>
        <div className="j-row-flex">
          <button className="j-btn primary" type="submit" disabled={busy}>
            Save links
          </button>
          <button className="j-btn ghost" type="button" onClick={add}>
            Add a link
          </button>
        </div>
      </form>
    </>
  );
}

export default function Links() {
  const [data, setData] = useState<LinksData | null>(null);
  const [saved, setSaved] = useState<LinkRow[]>([]);
  const [failed, setFailed] = useState<string>("");

  useEffect(() => {
    document.title = "links";
  }, []);

  useEffect(() => {
    let live = true;
    getJson<LinksData>("/links")
      .then((answer) => {
        if (live) {
          setData(answer);
          setSaved(answer.links);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setFailed(exc instanceof ApiError ? exc.message : "the links did not load");
        }
      });
    return () => {
      live = false;
    };
  }, []);

  if (failed !== "") {
    return (
      <Shell section={LINKS_SECTION}>
        <ErrorLine text={failed} />
      </Shell>
    );
  }
  if (data === null) {
    return (
      <Shell section={LINKS_SECTION}>
        <Spinner label="Loading the links" />
      </Shell>
    );
  }
  return (
    <Shell section={LINKS_SECTION}>
      <Page title="Links">
        <p className="j-muted">{ABOUT_TEXT}</p>
        <List links={saved} />
        {data.can_edit && <Editor data={data} onSaved={setSaved} />}
      </Page>
    </Shell>
  );
}

import type { FormEvent } from "react";

import ErrorLine from "./ErrorLine";

/**
 * The one editor of the viewer, for all three shapes it has: a wiki page, a
 * journal entry, and a new page.
 *
 * The title and the subtitle say what is edited. `keptFrontmatter` is the
 * frontmatter the save keeps byte for byte: it shows above the text and is
 * never in it. `people` makes the people field, which only a journal entry
 * has, and `name` makes the name field, which only a new page has. Each
 * field is the state of the page around the editor, so the page owns the
 * values it posts.
 */
export interface EditorProps {
  title: string;
  subtitle: string;
  keptFrontmatter?: string | null;
  body: string;
  onBody: (value: string) => void;
  people?: string | null;
  onPeople?: (value: string) => void;
  name?: string | null;
  onName?: (value: string) => void;
  error?: string;
  busy?: boolean;
  cancelUrl: string;
  onSave: () => void;
}

export default function Editor({
  title,
  subtitle,
  keptFrontmatter = null,
  body,
  onBody,
  people = null,
  onPeople,
  name = null,
  onName,
  error = "",
  busy = false,
  cancelUrl,
  onSave,
}: EditorProps) {
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onSave();
  }

  return (
    <>
      <h1 className="j-doc-title">{title}</h1>
      <p className="j-meta">{subtitle}</p>
      {error !== "" && <ErrorLine text={error} />}
      <form className="edit" onSubmit={submit}>
        {name !== null && (
          <label className="j-field-label">
            name (a file name; folders with a slash)
            <span className="j-field">
              <input
                name="name"
                value={name}
                placeholder="my-page.md"
                required
                onChange={(event) => onName?.(event.target.value)}
              />
            </span>
          </label>
        )}
        {people !== null && (
          <label className="j-field-label">
            people (ids, comma separated)
            <span className="j-field">
              <input
                name="people"
                value={people}
                onChange={(event) => onPeople?.(event.target.value)}
              />
            </span>
          </label>
        )}
        {keptFrontmatter !== null && keptFrontmatter !== "" && (
          <details className="kept">
            <summary>frontmatter, kept as it is</summary>
            <pre>{keptFrontmatter.trim()}</pre>
          </details>
        )}
        <label className="j-field-label">
          text
          <span className="j-field mono">
            <textarea
              name="body"
              rows={22}
              value={body}
              onChange={(event) => onBody(event.target.value)}
            />
          </span>
        </label>
        <div className="j-row-flex">
          <button className="j-btn primary" type="submit" disabled={busy}>
            save
          </button>
          <a className="j-btn ghost" href={cancelUrl}>
            cancel
          </a>
        </div>
      </form>
    </>
  );
}

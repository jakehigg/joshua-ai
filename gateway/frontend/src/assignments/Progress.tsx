/**
 * What the assignment did and what it waits for: the notice of an open
 * request, the outputs, and the checkpoints.
 *
 * The outputs are grouped by kind, in the order each kind first comes. An
 * output opens where it points: an `http` or `https` address, or the viewer
 * page of a document. Any other reference shows as code.
 */

import { Link } from "react-router-dom";

import type { Checkpoint, Notice as NoticeData, Output } from "./data";

/** The path of a document reference, `documents/<owner>/<slug>.md`. */
const DOCUMENT = /^documents\/([a-z0-9][a-z0-9-]*)\/([a-zA-Z0-9][a-zA-Z0-9._-]*)\.md$/;

/** True for an address the page may link to. */
export function isUrl(ref: string): boolean {
  try {
    const url = new URL(ref);
    return (url.protocol === "http:" || url.protocol === "https:") && url.host !== "";
  } catch {
    return false;
  }
}

/** The viewer page of a document reference, or null. */
export function documentUrl(ref: string): string | null {
  const match = DOCUMENT.exec(ref.trim());
  if (match === null) {
    return null;
  }
  return `/documents/${match[1] ?? ""}/${match[2] ?? ""}`;
}

/** What the assignment waits for: a request, a question, or the limits. */
export function Notice({ notice }: { notice: NoticeData | null }) {
  if (notice === null) {
    return null;
  }
  return (
    <aside className="a-notice warn" role="note">
      <strong>{notice.when === "" ? notice.heading : `${notice.heading} ${notice.when}`}</strong>
      {notice.ref !== "" && (
        <p className="a-text">
          <code>{notice.ref}</code>
        </p>
      )}
      {notice.text !== "" && <p className="a-text">{notice.text}</p>}
      {notice.foot !== "" && <p className="j-muted">{notice.foot}</p>}
    </aside>
  );
}

/** The title of one output, as a link when it has a place to open. */
function Title({ output }: { output: Output }) {
  const ref = output.ref.trim();
  const title = output.title === "" ? ref : output.title;
  const page = documentUrl(ref);
  if (isUrl(ref)) {
    return (
      <>
        <a className="a-out-title" href={ref} rel="noreferrer">
          {title}
        </a>
        <span className="a-out-ref">{new URL(ref).host}</span>
      </>
    );
  }
  if (output.kind.trim().toLowerCase() === "document" && page !== null) {
    return (
      <>
        <Link className="a-out-title" to={page}>
          {title}
        </Link>
        <code className="a-out-ref">{ref}</code>
      </>
    );
  }
  if (output.title !== "") {
    return (
      <>
        <span className="a-out-title">{output.title}</span>
        {ref !== "" && <code className="a-out-ref">{ref}</code>}
      </>
    );
  }
  return <code className="a-out-title">{ref}</code>;
}

/** The outputs, grouped by kind. */
export function Outputs({ outputs, title }: { outputs: Output[]; title: string }) {
  if (outputs.length === 0) {
    return null;
  }
  const groups = new Map<string, Output[]>();
  for (const output of outputs) {
    const key = output.kind.trim().toLowerCase();
    groups.set(key, [...(groups.get(key) ?? []), output]);
  }
  return (
    <section className="a-section" id="outputs">
      <div className="a-head">
        <h2>{title}</h2>
      </div>
      {[...groups.entries()].map(([key, group]) => (
        <div className="a-group" key={key}>
          <h3 className="j-label">
            {group[0]?.kind.trim() === "" ? "other" : group[0]?.kind.trim()}{" "}
            <span className="j-count">{group.length}</span>
          </h3>
          <ul className="a-cards a-outs">
            {group.map((output, index) => (
              <li className="a-out" key={`${output.ref}-${String(index)}`}>
                <div className="a-out-main">
                  <Title output={output} />
                  {output.when !== "" && <p className="a-out-note">{output.when}</p>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

/** The notes of the worker, newest first. */
export function Checkpoints({ checkpoints }: { checkpoints: Checkpoint[] }) {
  return (
    <section className="a-section" id="progress">
      <div className="a-head">
        <h2>Progress</h2>
      </div>
      {checkpoints.length === 0 ? (
        <p className="j-faint">No checkpoint yet.</p>
      ) : (
        <ol className="a-timeline">
          {checkpoints.map((point, index) => (
            <li key={`${point.at ?? ""}-${String(index)}`}>
              <span className="a-at">{point.when}</span>
              <p className="a-text">{point.note}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export default function Progress({
  outputs,
  checkpoints,
  done,
}: {
  outputs: Output[];
  checkpoints: Checkpoint[];
  done: boolean;
}) {
  return (
    <>
      <Outputs outputs={outputs} title={done ? "Outputs" : "Outputs so far"} />
      <Checkpoints checkpoints={checkpoints} />
    </>
  );
}

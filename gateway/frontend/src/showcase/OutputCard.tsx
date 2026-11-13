/**
 * One output of a report: the icon of its kind, the title, the reference and
 * the note.
 *
 * The server reads the Outputs section (`viewer_report.output_lines`), names
 * the icon of each kind (`viewer_api.assignments.kind_icon`) and says where
 * the output opens (`url`), so the card draws what it is given. A `url` with
 * a scheme is a place on the internet, and the card shows its host and sends
 * no referrer; any other `url` is a page of the viewer.
 *
 * The path data of an icon is a constant of the app, never a value from a
 * person and never a value from the server, so it goes into the element as
 * it is (`shell/Icon.tsx`).
 */

import Markdown from "../md/Markdown";
import type { ReportOutput } from "../assignments/data";

/** The icon of each kind of output: the name, and its paths on a 24px grid. */
export const KIND_ICONS: Record<string, string> = {
  task: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  page:
    '<path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>' +
    '<path d="M14 3v5h5M9 13h7M9 17h5"/>',
  issue: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="1.6"/>',
  "merge request":
    '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/>' +
    '<circle cx="18" cy="13" r="2.2"/><path d="M6 7.2v9.6M6 7.5c0 4 4 5.5 9.8 5.5"/>',
  journal:
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>' +
    '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  file: '<path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M13 3v6h6"/>',
  other: '<path d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/>',
};

/** True when the address is a place on the internet, not a page of the viewer. */
export function external(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

/** The host of an address, or "" when it has none. */
export function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

/** The icon of one kind of output. */
export function KindIcon({ icon }: { icon: string }) {
  return (
    <svg
      className="j-icon a-out-icon"
      viewBox="0 0 24 24"
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: KIND_ICONS[icon] ?? KIND_ICONS["other"] ?? "" }}
    />
  );
}

/** The title of the card, as a link when the output has a place to open. */
function Head({ output }: { output: ReportOutput }) {
  const ref = output.ref.trim();
  const title = output.title === "" ? ref : output.title;
  if (output.url !== null && external(output.url)) {
    return (
      <>
        <a className="a-out-title" href={output.url} rel="noreferrer">
          {title}
        </a>
        <span className="a-out-ref">{host(output.url)}</span>
      </>
    );
  }
  if (output.url !== null) {
    return (
      <>
        <a className="a-out-title" href={output.url}>
          {title}
        </a>
        {ref !== "" && <code className="a-out-ref">{ref}</code>}
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
  return ref === "" ? null : <code className="a-out-title">{ref}</code>;
}

export default function OutputCard({ output }: { output: ReportOutput }) {
  return (
    <>
      <KindIcon icon={output.icon} />
      <div className="a-out-main">
        <Head output={output} />
        {output.note !== "" && (
          <div className="a-out-note">
            <Markdown text={output.note} />
          </div>
        )}
      </div>
    </>
  );
}

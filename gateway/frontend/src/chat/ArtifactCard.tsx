// The document a turn wrote, as a card under the answer.
//
// A turn that creates or changes a document ends with one fenced block whose
// info string is `artifact`, holding the document path alone, on one line,
// in the form `documents/<owner>/<slug>.md`. Message.tsx takes that block
// out of the printed answer (blocks.ts) and hands the content
// here. The card shows the document's title, the path, and Open. The title
// comes from the viewer's own JSON route for the document
// (`getDocumentMeta`); while it is unknown, and when the call fails or 404s,
// the card shows the slug, exactly as it did before the title was read.
//
// Open shows the document in the panel of the chat screen
// (DocumentPanel.tsx), which frames the viewer's own page at `url`. The
// button is pressed while the panel shows this document, and closes it
// again. Outside a chat screen there is no panel, and Open opens nothing.
//
// A content that is not a path of that form shows one plain line instead of
// a card, so a card is never half built.

import { useMemo } from "react";
import { useDocumentPanel } from "./DocumentPanel";
import { useTitle } from "./useTitle";

export interface Artifact {
  path: string;
  slug: string;
  /**
   * The viewer's page for the document: a host-absolute path. It is a page
   * of the viewer, which the frame of the panel loads, so it never goes
   * through the router.
   */
  url: string;
}

// One path segment: a plain name, with no dot at its start and no `/`.
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export const MISSING_LINE = "Joshua pointed at a document that is not there";

// The document of an `artifact` block's content, or null when the content is
// not one `documents/<owner>/<slug>.md` path.
export function parseArtifact(content: string): Artifact | null {
  const path = content.trim();
  if (!path || /\s/.test(path)) return null;
  const parts = path.split("/");
  if (parts.length !== 3 || parts[0] !== "documents") return null;
  const owner = parts[1] as string;
  const file = parts[2] as string;
  if (!NAME.test(owner) || !file.endsWith(".md")) return null;
  const slug = file.slice(0, -".md".length);
  if (!NAME.test(slug)) return null;
  // The viewer serves the document at /documents/<owner>/<slug>, the path
  // without its .md.
  return { path, slug, url: `/${path.slice(0, -".md".length)}` };
}

export function ArtifactCard({ content }: { content: string }) {
  const artifact = useMemo(() => parseArtifact(content), [content]);
  const title = useTitle(artifact?.url);
  const panel = useDocumentPanel();
  if (!artifact) return <p className="artifact-missing">{MISSING_LINE}</p>;
  const open = panel.shown?.url === artifact.url;
  return (
    <div className="artifact-card">
      <span className="artifact-icon" aria-hidden="true">
        📄
      </span>
      <span className="artifact-text">
        {/* The first line is the title once it is known, the slug until
            then; the path stays the second line either way. */}
        <span className="artifact-slug">{title ?? artifact.slug}</span>
        <span className="artifact-path">{artifact.path}</span>
      </span>
      <button
        type="button"
        className="artifact-open"
        aria-pressed={open}
        onClick={() => (open ? panel.close() : panel.show(artifact))}
      >
        Open
      </button>
    </div>
  );
}

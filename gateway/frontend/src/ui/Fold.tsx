import type { ReactNode } from "react";

/**
 * One folded section: a `<details>` with a title and a short hint in its
 * summary, closed unless `open`.
 *
 * The classes are the ones `viewer.css` defines for a fold of the
 * assignments and the documents, so the look is the look of the viewer. The
 * Source fold and the Versions fold of a document use it.
 */
export default function Fold({
  title,
  hint,
  open = false,
  children,
}: {
  title: string;
  hint?: string;
  open?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="a-fold" open={open}>
      <summary>
        <span className="a-fold-title">{title}</span>
        {hint !== undefined && hint !== "" && <span className="a-fold-hint">{hint}</span>}
      </summary>
      <div className="a-fold-body">{children}</div>
    </details>
  );
}

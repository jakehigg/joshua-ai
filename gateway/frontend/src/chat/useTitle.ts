import { useEffect, useState } from "react";
import { getDocumentMeta } from "../api/client";

// The document's own title, or null while it is unknown. One call per card,
// when the card appears. A call that fails, a 404 for a document that is not
// this person's, and an empty title all leave it null, and the card then
// stands as it stood before: no error state, no empty line.
export function useTitle(url: string | undefined): string | null {
  const [title, setTitle] = useState<string | null>(null);
  useEffect(() => {
    setTitle(null);
    if (!url) return undefined;
    const controller = new AbortController();
    void getDocumentMeta(url, controller.signal).then((meta) => {
      const name = meta?.title.trim();
      if (name && !controller.signal.aborted) setTitle(name);
    });
    return () => controller.abort();
  }, [url]);
  return title;
}


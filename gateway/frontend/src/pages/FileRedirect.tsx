import { useEffect } from "react";

import Shell from "../shell/Shell";
import Spinner from "../ui/Spinner";

/** The answer of `GET /api/v1/wiki` for a file that is not a page. */
export interface WikiFileData {
  kind: "file";
  rel: string;
  name: string;
  url: string;
  inline: boolean;
}

/**
 * A file that is not a page: the browser goes to the route that delivers the
 * bytes. An image shows inline there, anything else downloads with its name.
 * The address of the file replaces this one in the history, so every
 * bookmarked `/wiki/…`, `/shared/…` and `/attachments/…` address still works.
 */
export default function FileRedirect({ file }: { file: WikiFileData }) {
  useEffect(() => {
    window.location.replace(file.url);
  }, [file.url]);

  return (
    <Shell>
      <Spinner label={`Opening ${file.name}`} />
    </Shell>
  );
}

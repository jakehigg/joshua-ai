import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useParams } from "react-router-dom";

import { ApiError, getJson } from "./api/client";
import Edit from "./pages/Edit";
import FileRedirect, { type WikiFileData } from "./pages/FileRedirect";
import Journal from "./pages/Journal";
import JournalDay from "./pages/JournalDay";
import Me from "./pages/Me";
import New from "./pages/New";
import WikiFolder, { type WikiFolderData } from "./pages/WikiFolder";
import WikiPage, { type WikiPageData } from "./pages/WikiPage";
import Shell from "./shell/Shell";
import ErrorLine from "./ui/ErrorLine";
import Spinner from "./ui/Spinner";

/** The answer of `GET /api/v1/wiki`, by what the path names. */
type Answer = WikiPageData | WikiFolderData | WikiFileData;

/**
 * One address of the content: the page, the folder, or the file at it.
 *
 * The splat of the route is the path under the root, so the address is the
 * root-relative path the JSON route reads. A trailing slash and a path that
 * names a directory both answer a folder, because the route answers by what
 * the path is.
 */
function Content({ root }: { root: string }) {
  const splat = useParams()["*"] ?? "";
  const path = `${root}/${splat}`;
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setAnswer(null);
    setError("");
    getJson<Answer>(`/wiki?path=${encodeURIComponent(path)}`)
      .then((body) => {
        if (live) {
          setAnswer(body);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the page did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [path]);

  if (error !== "") {
    return (
      <Shell section={root === "wiki" ? "wiki" : ""}>
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (answer === null) {
    return (
      <Shell section={root === "wiki" ? "wiki" : ""}>
        <Spinner label="Loading the page" />
      </Shell>
    );
  }
  if (answer.kind === "page") {
    return <WikiPage page={answer} />;
  }
  if (answer.kind === "folder") {
    return <WikiFolder folder={answer} />;
  }
  return <FileRedirect file={answer} />;
}

/**
 * The routes of the app. Only a page that is migrated is here: every other
 * address of the viewer is still a page of the server renderer, so there is
 * no catch-all route that would swallow it.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/wiki/" replace />} />
      <Route path="/wiki/*" element={<Content root="wiki" />} />
      <Route path="/shared/*" element={<Content root="shared" />} />
      <Route path="/attachments/*" element={<Content root="attachments" />} />
      <Route path="/journal" element={<Journal />} />
      <Route path="/journal/:year/:month/:day" element={<JournalDay />} />
      <Route path="/edit/*" element={<Edit />} />
      <Route path="/new" element={<New />} />
      <Route path="/me" element={<Me />} />
    </Routes>
  );
}

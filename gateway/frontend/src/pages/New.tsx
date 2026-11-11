import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, getJson, postJson } from "../api/client";
import Shell from "../shell/Shell";
import Editor from "../ui/Editor";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";
import type { SaveAnswer } from "./Edit";

/** The answer of `GET /api/v1/new`. */
export interface NewData {
  folder: string;
  cancel_url: string;
  csrf: string;
}

/**
 * `/new?folder=<wiki folder>`: a new page in a folder of the wiki.
 *
 * The name field takes a file name, or a path of plain names under the
 * folder. `POST /api/v1/save` with the folder and the name writes the page
 * and says where it is; a name that is already taken, or one that is not a
 * name, keeps the editor and shows what the viewer said.
 */
export default function New() {
  const folder = useSearchParams()[0].get("folder") ?? "";
  const navigate = useNavigate();
  const [data, setData] = useState<NewData | null>(null);
  const [name, setName] = useState<string>("");
  const [body, setBody] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [failed, setFailed] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    let live = true;
    document.title = "new page";
    getJson<NewData>(`/new?folder=${encodeURIComponent(folder)}`)
      .then((answer) => {
        if (live) {
          setData(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setFailed(exc instanceof ApiError ? exc.message : "the editor did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [folder]);

  async function save(): Promise<void> {
    if (data === null) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const answer = await postJson<SaveAnswer>("/save", {
        csrf: data.csrf,
        folder: data.folder,
        name,
        body,
      });
      navigate(answer.url);
    } catch (exc: unknown) {
      setBusy(false);
      setError(exc instanceof ApiError ? exc.message : "the page was not saved");
    }
  }

  if (failed !== "") {
    return (
      <Shell section="wiki">
        <ErrorLine text={failed} />
      </Shell>
    );
  }
  if (data === null) {
    return (
      <Shell section="wiki">
        <Spinner label="Loading the editor" />
      </Shell>
    );
  }
  return (
    <Shell section="wiki">
      <Editor
        title="new page"
        subtitle={`in ${data.folder === "" ? "the wiki" : data.folder}`}
        body={body}
        onBody={setBody}
        name={name}
        onName={setName}
        error={error}
        busy={busy}
        cancelUrl={data.cancel_url}
        onSave={save}
      />
    </Shell>
  );
}

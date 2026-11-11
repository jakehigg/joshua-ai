import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError, getJson, postJson } from "../api/client";
import Shell from "../shell/Shell";
import Editor from "../ui/Editor";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";

/** The answer of `GET /api/v1/edit`. */
export interface EditData {
  kind: "wiki" | "journal";
  rel: string;
  title: string;
  subtitle: string;
  body: string;
  kept_frontmatter: string | null;
  people: string[] | null;
  cancel_url: string;
  csrf: string;
}

/** The answer of a write: where to go now. */
export interface SaveAnswer {
  ok: boolean;
  url: string;
}

/**
 * `/edit/<wiki-relative path>`: the editor of one wiki page or journal entry.
 *
 * The page reads the text from `GET /api/v1/edit`, posts the change to
 * `POST /api/v1/save` with the token of that answer, and goes where the
 * answer says: a wiki page is an address of this app, and the day of a
 * journal entry is a page of the server renderer.
 */
export default function Edit() {
  const rel = useParams()["*"] ?? "";
  const navigate = useNavigate();
  const [data, setData] = useState<EditData | null>(null);
  const [body, setBody] = useState<string>("");
  const [people, setPeople] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [failed, setFailed] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    let live = true;
    getJson<EditData>(`/edit?path=${encodeURIComponent(rel)}`)
      .then((answer) => {
        if (live) {
          setData(answer);
          setBody(answer.body);
          setPeople(answer.people === null ? "" : answer.people.join(", "));
          document.title = `edit ${answer.title}`;
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
  }, [rel]);

  const section = data !== null && data.kind === "journal" ? "journal" : "wiki";

  async function save(): Promise<void> {
    if (data === null) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const answer = await postJson<SaveAnswer>("/save", {
        csrf: data.csrf,
        path: data.rel,
        body,
        people,
      });
      if (data.kind === "wiki") {
        navigate(answer.url);
      } else {
        window.location.assign(answer.url);
      }
    } catch (exc: unknown) {
      setBusy(false);
      setError(exc instanceof ApiError ? exc.message : "the page was not saved");
    }
  }

  if (failed !== "") {
    return (
      <Shell section={section}>
        <ErrorLine text={failed} />
      </Shell>
    );
  }
  if (data === null) {
    return (
      <Shell section={section}>
        <Spinner label="Loading the editor" />
      </Shell>
    );
  }
  return (
    <Shell section={section}>
      <Editor
        title={`edit: ${data.title}`}
        subtitle={data.subtitle}
        keptFrontmatter={data.kept_frontmatter}
        body={body}
        onBody={setBody}
        people={data.people === null ? null : people}
        onPeople={setPeople}
        error={error}
        busy={busy}
        cancelUrl={data.cancel_url}
        onSave={save}
      />
    </Shell>
  );
}

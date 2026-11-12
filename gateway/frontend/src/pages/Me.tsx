import { useEffect, useState } from "react";

import { ApiError, getJson, type Person } from "../api/client";
import Shell from "../shell/Shell";
import WikiTree from "../shell/WikiTree";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** One attachment of the person, as `GET /api/v1/me` sends it. */
export interface Attachment {
  path: string;
  url: string;
}

/** The answer of `GET /api/v1/me`. */
export interface MePerson {
  person: Person;
  profile_url: string | null;
  attachments: Attachment[];
}

function Attachments({ items }: { items: Attachment[] }) {
  return (
    <>
      <h2 className="j-label">attachments</h2>
      {items.length === 0 ? (
        <Empty />
      ) : (
        <ul className="tree">
          {items.map((item) => (
            <li key={item.path}>
              <a href={item.url}>{item.path}</a>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * `/me`: the page of the person who signed in, with the link to their
 * profile, the link to the journal, their recent attachments and the wiki
 * tree. The anonymous identity is no person on disk, so it gets no profile
 * link and no attachments.
 */
export default function Me() {
  const [me, setMe] = useState<MePerson | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    getJson<MePerson>("/me")
      .then((answer) => {
        if (live) {
          setMe(answer);
          document.title = answer.person.name;
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
  }, []);

  if (error !== "") {
    return (
      <Shell section="me">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (me === null) {
    return (
      <Shell section="me">
        <Spinner label="Loading your page" />
      </Shell>
    );
  }
  // The anonymous identity is the one person with no profile page, and it has
  // no attachments folder either.
  const anonymous = me.profile_url === null;
  return (
    <Shell section="me">
      <Page title={me.person.name}>
        <p className="j-meta">
          {me.profile_url !== null && (
            <>
              <a href={me.profile_url}>profile</a> ·{" "}
            </>
          )}
          <a href="/journal">journal</a>
        </p>
        {!anonymous && <Attachments items={me.attachments} />}
        <h2 className="j-label">wiki</h2>
        <WikiTree />
      </Page>
    </Shell>
  );
}

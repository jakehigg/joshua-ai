/**
 * `/search?q=<query>`: the search of the wiki, by meaning and by words.
 *
 * The query is the address, so a bookmark of a search opens the same hits.
 * `GET /api/v1/search` runs both halves: the search by meaning through core,
 * and the substring search over the Markdown the person can read. The words
 * half always shows, so the page works when core is down or not configured;
 * then the meaning half is absent and the page says nothing about it.
 *
 * A hit links the page it names, which can be a page of this app or one the
 * server renders, so it is a plain link and the browser loads the address.
 */

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import SearchField from "../shell/SearchField";
import Shell from "../shell/Shell";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** One hit of either half. A hit by words carries no `source`. */
export interface SearchHit {
  url: string;
  label: string;
  source?: string;
  snippet: string;
}

/** The answer of `GET /api/v1/search`. */
export interface SearchAnswer {
  query: string;
  meaning_configured: boolean;
  meaning: SearchHit[] | null;
  words: SearchHit[];
}

/** What the page says with no query. */
export const ASK_TEXT = "Enter a query.";

/** What the meaning half says when core found nothing close. */
export const NO_CLOSE_TEXT = "No close match.";

function Hits({ hits }: { hits: SearchHit[] }) {
  return (
    <ul className="j-list">
      {hits.map((hit, index) => (
        <li className="j-list-row" key={`${hit.url}-${index}`}>
          <div className="j-list-main">
            <a className="j-list-title wrap" href={hit.url}>
              {hit.label}
            </a>
            {hit.source !== undefined && hit.source !== "" && (
              <span className="j-list-sub">{hit.source}</span>
            )}
            {hit.snippet !== "" && <span className="j-list-sub hit-text">{hit.snippet}</span>}
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function Search() {
  const [params] = useSearchParams();
  const query = (params.get("q") ?? "").trim();
  const [answer, setAnswer] = useState<SearchAnswer | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    document.title = "search";
  }, []);

  useEffect(() => {
    setAnswer(null);
    setError("");
    if (query === "") {
      return;
    }
    let live = true;
    getJson<SearchAnswer>(`/search?q=${encodeURIComponent(query)}`)
      .then((body) => {
        if (live) {
          setAnswer(body);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the search did not run");
        }
      });
    return () => {
      live = false;
    };
  }, [query]);

  if (query === "") {
    return (
      <Shell section="search">
        <SearchField query={query} />
        <Page title="search">
          <Empty text={ASK_TEXT} />
        </Page>
      </Shell>
    );
  }
  if (error !== "") {
    return (
      <Shell section="search">
        <SearchField query={query} />
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (answer === null) {
    return (
      <Shell section="search">
        <SearchField query={query} />
        <Spinner label="Searching" />
      </Shell>
    );
  }
  const words =
    answer.words.length === 0 ? (
      <Empty text={`No match for ${query}.`} />
    ) : (
      <Hits hits={answer.words} />
    );
  return (
    <Shell section="search">
      <SearchField query={query} />
      <Page title="search">
        {answer.meaning !== null && (
          <section className="meaning">
            <h2>By meaning</h2>
            {answer.meaning.length === 0 ? (
              <Empty text={NO_CLOSE_TEXT} />
            ) : (
              <Hits hits={answer.meaning} />
            )}
          </section>
        )}
        {answer.meaning === null ? (
          words
        ) : (
          <section className="words">
            <h2>By words</h2>
            {words}
          </section>
        )}
      </Page>
    </Shell>
  );
}

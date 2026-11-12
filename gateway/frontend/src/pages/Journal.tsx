/**
 * `/journal`: one month of the journal, newest day first.
 *
 * The month picker and the person filter are the query of the address
 * (`?month=YYYY-MM&person=<id>`), so a bookmark opens the same month of the
 * same person. The route answers which months hold an entry and who the
 * people are, and the default month is the newest month with an entry.
 */

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Entry, { monthLabel, type EntryPerson, type JournalEntry } from "../journal/Entry";
import Shell from "../shell/Shell";
import Chip from "../ui/Chip";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** The answer of `GET /api/v1/journal`. */
export interface JournalFeed {
  month: string;
  available: string[];
  people: EntryPerson[];
  person: string | null;
  can_edit: boolean;
  entries: JournalEntry[];
}

/** What an empty month says. */
export const EMPTY_TEXT =
  "Nothing in the journal yet. Joshua writes an entry when something is worth " +
  "keeping, and a page for the day at the nightly run.";

/** The months of the picker: the ones with an entry, and the month shown. */
function choices(feed: JournalFeed): string[] {
  if (feed.available.includes(feed.month)) {
    return feed.available;
  }
  return [feed.month, ...feed.available].sort().reverse();
}

export default function Journal() {
  const [params, setParams] = useSearchParams();
  const month = params.get("month") ?? "";
  const person = params.get("person") ?? "";
  const [feed, setFeed] = useState<JournalFeed | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setFeed(null);
    setError("");
    const query = new URLSearchParams();
    if (month !== "") {
      query.set("month", month);
    }
    if (person !== "") {
      query.set("person", person);
    }
    const suffix = query.toString();
    getJson<JournalFeed>(suffix === "" ? "/journal" : `/journal?${suffix}`)
      .then((answer) => {
        if (live) {
          setFeed(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the journal did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [month, person]);

  useEffect(() => {
    document.title = "journal";
  }, []);

  /** Write one filter into the address; an empty value drops it. */
  function pick(name: string, value: string): void {
    const next = new URLSearchParams(params);
    if (value === "") {
      next.delete(name);
    } else {
      next.set(name, value);
    }
    setParams(next);
  }

  if (error !== "") {
    return (
      <Shell section="journal">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (feed === null) {
    return (
      <Shell section="journal">
        <Spinner label="Loading the journal" />
      </Shell>
    );
  }
  return (
    <Shell section="journal">
      <Page title="journal">
        <p className="pager">
          <label className="j-field">
            <select
              value={feed.month}
              aria-label="Month"
              onChange={(event) => pick("month", event.target.value)}
            >
              {choices(feed).map((item) => (
                <option key={item} value={item}>
                  {monthLabel(item)}
                </option>
              ))}
            </select>
          </label>
        </p>
        {feed.people.length > 0 && (
          <div className="j-chips" role="group" aria-label="People">
            <Chip on={feed.person === null} onClick={() => pick("person", "")}>
              everyone
            </Chip>
            {feed.people.map((item) => (
              <Chip
                key={item.id}
                on={item.id === feed.person}
                onClick={() => pick("person", item.id)}
              >
                {item.name}
              </Chip>
            ))}
          </div>
        )}
        {feed.entries.length === 0 ? (
          <Empty text={EMPTY_TEXT} />
        ) : (
          feed.entries.map((entry) => (
            <Entry key={entry.day_url + entry.slug} entry={entry} canEdit={feed.can_edit} />
          ))
        )}
      </Page>
    </Shell>
  );
}

/**
 * `/journal/<year>/<month>/<day>`: one day of the journal.
 *
 * The page shows the entries of that day and the nearest day with an entry on
 * each side, so a person reads the journal day by day. The address of an
 * entry is this page and the slug of the entry as its fragment, which is
 * where the editor comes back to.
 */

import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Entry, { dayLabel, monthLabel, type JournalEntry } from "../journal/Entry";
import Shell from "../shell/Shell";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";

/** The nearest day with an entry on one side of a day. */
export interface DayHop {
  day: string;
  url: string;
}

/** The answer of `GET /api/v1/journal/<year>/<month>/<day>`. */
export interface JournalDayData {
  day: string;
  can_edit: boolean;
  entries: JournalEntry[];
  older: DayHop | null;
  newer: DayHop | null;
}

/** What a day with no entry says. */
export const EMPTY_TEXT = "Nothing on this day.";

/** The address of the feed, on the month of a day. */
function monthUrl(day: string): string {
  return `/journal?month=${day.slice(0, 7)}`;
}

export default function JournalDay() {
  const { year = "", month = "", day = "" } = useParams();
  const hash = useLocation().hash;
  const [data, setData] = useState<JournalDayData | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setData(null);
    setError("");
    getJson<JournalDayData>(`/journal/${year}/${month}/${day}`)
      .then((answer) => {
        if (live) {
          setData(answer);
          document.title = answer.day;
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the day did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [year, month, day]);

  // The entries arrive after the address, so the fragment of an entry scrolls
  // to it once they are here.
  useEffect(() => {
    if (data === null || hash === "") {
      return;
    }
    document.getElementById(hash.slice(1))?.scrollIntoView();
  }, [data, hash]);

  if (error !== "") {
    return (
      <Shell section="journal">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (data === null) {
    return (
      <Shell section="journal">
        <Spinner label="Loading the day" />
      </Shell>
    );
  }
  return (
    <Shell section="journal">
      <Page title={dayLabel(data.day)}>
        <p className="pager">
          {data.older !== null && (
            <Link to={data.older.url}>← {dayLabel(data.older.day)}</Link>
          )}
          {data.older !== null && " · "}
          <Link to={monthUrl(data.day)}>{monthLabel(data.day.slice(0, 7))}</Link>
          {data.newer !== null && " · "}
          {data.newer !== null && (
            <Link to={data.newer.url}>{dayLabel(data.newer.day)} →</Link>
          )}
        </p>
        {data.entries.length === 0 ? (
          <Empty text={EMPTY_TEXT} />
        ) : (
          data.entries.map((entry) => (
            <Entry key={entry.slug} entry={entry} canEdit={data.can_edit} />
          ))
        )}
      </Page>
    </Shell>
  );
}

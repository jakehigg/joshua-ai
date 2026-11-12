/**
 * One entry of the journal: the nightly page of a day, or an entry the agent
 * wrote that day.
 *
 * The title links the entry on its day, the day links the day, the badge says
 * who wrote it, and a chip names each person the entry names. A member gets
 * the editor of the entry, which edits the text and the people only.
 *
 * The classes are the ones `joshua-ui.css` and `viewer.css` already define
 * for an entry, so the look is the look of the viewer.
 */

import { Link } from "react-router-dom";

import Markdown from "../md/Markdown";

/** One person an entry names, as the journal routes send them. */
export interface EntryPerson {
  id: string;
  name: string;
}

/** One entry, as `GET /api/v1/journal` and the day route send it. */
export interface JournalEntry {
  day: string;
  day_url: string;
  slug: string;
  title: string;
  source: string;
  people: EntryPerson[];
  markdown: string;
  edit_url: string;
}

/** The source of the page of a day: the nightly run, not the agent. */
const NIGHTLY = "nightly";

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/**
 * `Friday 4 September 2026`, from a `YYYY-MM-DD` day. The names are the names
 * of the viewer, which is English, so every browser says the same thing.
 */
export function dayLabel(day: string): string {
  const when = new Date(`${day}T00:00:00`);
  const weekday = WEEKDAYS[when.getDay()];
  const month = MONTHS[when.getMonth()];
  return `${weekday} ${when.getDate()} ${month} ${when.getFullYear()}`;
}

/** `September 2026`, from a `YYYY-MM` month. */
export function monthLabel(month: string): string {
  const when = new Date(`${month}-01T00:00:00`);
  return `${MONTHS[when.getMonth()]} ${when.getFullYear()}`;
}

/** The address of the feed, filtered on one person. */
function personUrl(id: string): string {
  return `/journal?person=${encodeURIComponent(id)}`;
}

export default function Entry({
  entry,
  canEdit,
}: {
  entry: JournalEntry;
  canEdit: boolean;
}) {
  return (
    <article className="j-entry" id={entry.slug}>
      <header className="entry-head">
        <h3 className="j-entry-title">
          <Link to={`${entry.day_url}#${entry.slug}`}>{entry.title}</Link>
        </h3>
        <span className={entry.source === NIGHTLY ? "j-badge accent" : "j-badge"}>
          {entry.source}
        </span>
      </header>
      <p className="j-entry-when">
        <Link to={entry.day_url}>{dayLabel(entry.day)}</Link>
      </p>
      {entry.people.length > 0 && (
        <div className="j-chips" role="group" aria-label="People">
          {entry.people.map((person) => (
            <Link className="j-chip" key={person.id} to={personUrl(person.id)}>
              {person.name}
            </Link>
          ))}
        </div>
      )}
      {entry.markdown.trim() === "" ? (
        <p className="j-faint">(empty)</p>
      ) : (
        <Markdown text={entry.markdown} />
      )}
      {canEdit && (
        <footer className="j-meta">
          <a href={entry.edit_url}>edit this entry</a>
        </footer>
      )}
    </article>
  );
}

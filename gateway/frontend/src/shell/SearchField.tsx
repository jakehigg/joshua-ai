/**
 * The search field of the search page. The app bar has no search field.
 *
 * A submit goes to `/search?q=<query>` with the router, so no page load
 * happens: the search page reads the address and asks
 * `GET /api/v1/search?q=`.
 *
 * `query` is the query of the address, which the search page passes back, so
 * the field holds what the page shows.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import Icon from "./Icon";
import { SEARCH_ICON } from "./sections";

/** The address of the search of one query. */
export function searchUrl(query: string): string {
  return `/search?q=${encodeURIComponent(query)}`;
}

export default function SearchField({ query = "" }: { query?: string }) {
  const navigate = useNavigate();
  const [text, setText] = useState(query);

  // The address is what the page shows, so a new one replaces the text.
  useEffect(() => {
    setText(query);
  }, [query]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    navigate(searchUrl(text.trim()));
  }

  return (
    <form className="v-search" role="search" onSubmit={submit}>
      <label className="j-field">
        <Icon paths={SEARCH_ICON} />
        <input
          type="search"
          name="q"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Search"
          aria-label="Search"
        />
      </label>
    </form>
  );
}

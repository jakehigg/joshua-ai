import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import SearchField, { searchUrl } from "./SearchField";

/** The address of the app, so a test reads where the field went. */
function Address() {
  const location = useLocation();
  return <p>{`at ${location.pathname}${location.search}`}</p>;
}

function field(query = "") {
  return render(
    <MemoryRouter initialEntries={["/wiki/"]}>
      <SearchField query={query} />
      <Address />
    </MemoryRouter>,
  );
}

function input(): HTMLInputElement {
  return document.querySelector('form.v-search input[name="q"]') as HTMLInputElement;
}

describe("the search field of the app bar", () => {
  it("keeps the markup of the app bar the server renders", () => {
    field();

    const form = document.querySelector("form.v-search");
    expect(form?.getAttribute("role")).toEqual("search");
    expect(form?.querySelector("label.j-field svg")).not.toBeNull();
    expect(input().type).toEqual("search");
    expect(input().getAttribute("aria-label")).toEqual("Search");
  });

  it("goes to the search page of the query, and loads no page", () => {
    field();

    fireEvent.change(input(), { target: { value: "  pizza dough  " } });
    fireEvent.submit(document.querySelector("form.v-search") as HTMLFormElement);

    expect(screen.getByText("at /search?q=pizza%20dough")).toBeDefined();
  });

  it("holds the query of the address", () => {
    field("oats");

    expect(input().value).toEqual("oats");
  });

  it("builds the address of one query", () => {
    expect(searchUrl("a b&c")).toEqual("/search?q=a%20b%26c");
  });
});

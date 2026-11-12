import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import LinksNav, { type LinksData } from "./LinksNav";

const ANSWER: LinksData = {
  links: [
    { title: "Weather", url: "https://example.com/weather", icon: "cloud" },
    { title: "Pantry", url: "https://pantry.example.test/", icon: "box" },
  ],
  icons: ["link", "box", "cloud"],
  can_edit: true,
  csrf: "a-token",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubLinks(answer: Response = json(ANSWER)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    return Promise.resolve(answer);
  });
  return asked;
}

function nav(section = "") {
  return render(
    <MemoryRouter>
      <LinksNav section={section} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the links of the sidebar", () => {
  it("reads the one source of the list", async () => {
    const asked = stubLinks();

    nav();

    expect(await screen.findByText("Weather")).toBeDefined();
    expect(asked).toEqual(["/api/v1/links"]);
  });

  it("draws one row per link, with its icon, in a new tab", async () => {
    stubLinks();

    nav();

    const weather = (await screen.findByText("Weather")).closest("a");
    expect(weather?.getAttribute("href")).toEqual("https://example.com/weather");
    expect(weather?.getAttribute("target")).toEqual("_blank");
    expect(weather?.getAttribute("rel")).toEqual("noopener noreferrer");
    expect(weather?.querySelector("svg.j-icon")).not.toBeNull();
    expect(document.querySelectorAll("nav.v-links .j-nav-item").length).toEqual(3);
  });

  it("goes to the page of the links, and marks it as the current one", async () => {
    stubLinks();

    nav("links");

    const edit = await screen.findByText("Edit links");
    expect(edit.getAttribute("href")).toEqual("/links");
    expect(edit.getAttribute("aria-current")).toEqual("page");
  });

  it("marks nothing on another section", async () => {
    stubLinks();

    nav("wiki");

    expect((await screen.findByText("Edit links")).getAttribute("aria-current")).toBeNull();
  });

  it("asks to add a link when there is none", async () => {
    stubLinks(json({ ...ANSWER, links: [] }));

    nav();

    expect(await screen.findByText("Add links")).toBeDefined();
  });

  it("still offers the page when the list does not load", async () => {
    stubLinks(json({ error: "not found" }, 404));

    nav();

    expect(await screen.findByText("Add links")).toBeDefined();
  });
});

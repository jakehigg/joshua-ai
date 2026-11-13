import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Hero from "./Hero";

describe("the head of a showcase", () => {
  it("gives the marks, the title, the lede and the numbers, in that order", () => {
    render(
      <Hero
        title="Clean up the docs"
        lede="Every page of the wiki reads the same now."
        chips={<span className="j-badge ok">done</span>}
        tiles={[{ value: "3", label: "decisions" }]}
      />,
    );

    const hero = document.querySelector("header.a-hero") as HTMLElement;
    expect([...hero.children].map((part) => part.className)).toEqual([
      "a-chips",
      "j-doc-title",
      "a-lede",
      "j-stats a-stats",
    ]);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toEqual("Clean up the docs");
    expect(hero.querySelector(".j-stat b")?.textContent).toEqual("3");
  });

  it("gives no title, no lede and no numbers when a page has none", () => {
    render(<Hero title="" />);

    const hero = document.querySelector("header.a-hero") as HTMLElement;
    expect(hero.children.length).toEqual(0);
  });

  it("takes the class, the id, more under the lede, and what only one page has", () => {
    render(
      <Hero
        title="Party plan"
        className="a-hero d-hero"
        id="L0"
        sub={<p className="a-start-line">Say "start assignment 9".</p>}
      >
        <button type="button">+</button>
      </Hero>,
    );

    const hero = document.querySelector("header") as HTMLElement;
    expect(hero.className).toEqual("a-hero d-hero");
    expect(hero.id).toEqual("L0");
    expect(screen.getByText('Say "start assignment 9".')).toBeDefined();
    expect(screen.getByRole("button").textContent).toEqual("+");
  });
});

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Tiles, { count, type TileData } from "./Tiles";

const TILES: TileData[] = [
  { value: "12k tokens", label: "cost", title: "12,000 tokens" },
  { value: "", label: "working time" },
  { value: "sonnet", label: "model", text: true },
];

describe("the number tiles of a showcase", () => {
  it("gives one tile a number, with its label and its hint", () => {
    render(<Tiles tiles={TILES} />);

    const row = document.querySelector(".j-stats.a-stats") as HTMLElement;
    expect(row.getAttribute("role")).toEqual("group");
    expect(row.getAttribute("aria-label")).toEqual("Numbers");
    const tiles = [...row.querySelectorAll(".j-stat")];
    expect(tiles.map((tile) => tile.textContent)).toEqual(["12k tokenscost", "sonnetmodel"]);
    expect(tiles[0]?.getAttribute("title")).toEqual("12,000 tokens");
  });

  it("sets a value of words in the smaller size", () => {
    render(<Tiles tiles={TILES} />);

    expect(document.querySelector(".a-stat-text b")?.textContent).toEqual("sonnet");
  });

  it("shows nothing when every tile is empty", () => {
    render(<Tiles tiles={[{ value: "", label: "cost" }]} label="Of the run" />);

    expect(document.querySelector(".j-stats")).toBeNull();
  });

  it("takes the label of the group", () => {
    render(<Tiles tiles={TILES} label="Of the run" />);

    expect(document.querySelector(".j-stats")?.getAttribute("aria-label")).toEqual("Of the run");
  });

  it("counts in the words of a person", () => {
    expect(count(1, "decision")).toEqual("1 decision");
    expect(count(3, "decision")).toEqual("3 decisions");
  });
});

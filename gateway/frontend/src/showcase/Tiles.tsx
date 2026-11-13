/**
 * The numbers at the top of a showcase: one tile a number with its label.
 *
 * The rules of `viewer_showcase.tile` and `viewer_showcase.tiles`: a tile
 * with no value shows nothing, a tile of text is smaller than a number, and
 * the row is a group with a label. An assignment and a document count
 * different things, so each page builds its own tiles and passes them here.
 */

/** One number with its label, and the hint of its title. */
export interface TileData {
  value: string;
  label: string;
  title?: string;
  /** The value is words, not a number, so it is set in the smaller size. */
  text?: boolean;
}

/** `3 decisions`, `1 decision`: the label of a tile that counts. */
export function count(n: number, noun: string): string {
  return `${String(n)} ${noun}${n === 1 ? "" : "s"}`;
}

export function Tile({ tile }: { tile: TileData }) {
  if (tile.value === "") {
    return null;
  }
  return (
    <div className={tile.text === true ? "j-stat a-stat-text" : "j-stat"} title={tile.title}>
      <b>{tile.value}</b>
      <span>{tile.label}</span>
    </div>
  );
}

export default function Tiles({ tiles, label = "Numbers" }: { tiles: TileData[]; label?: string }) {
  if (tiles.every((tile) => tile.value === "")) {
    return null;
  }
  return (
    <div className="j-stats a-stats" role="group" aria-label={label}>
      {tiles.map((tile) => (
        <Tile key={tile.label} tile={tile} />
      ))}
    </div>
  );
}

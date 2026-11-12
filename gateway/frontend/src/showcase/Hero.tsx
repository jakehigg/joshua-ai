/**
 * The head of a showcase: the marks, the title, the lede, and the numbers.
 *
 * An assignment and a document show different marks and count different
 * things, so each page passes its chips and its tiles. `sub` is more under
 * the lede, which a draft uses for the words that start it, and `children`
 * is what only one of them has: the properties of a document, its "+" and
 * its cards.
 */

import type { ReactNode } from "react";

import Tiles, { type TileData } from "./Tiles";

export default function Hero({
  title,
  lede = "",
  ledeClass = "a-lede",
  chips,
  sub,
  tiles = [],
  className = "a-hero",
  id,
  children,
}: {
  title: string;
  lede?: string;
  ledeClass?: string;
  chips?: ReactNode;
  sub?: ReactNode;
  tiles?: TileData[];
  className?: string;
  id?: string;
  children?: ReactNode;
}) {
  return (
    <header className={className} id={id}>
      {chips !== undefined && <div className="a-chips">{chips}</div>}
      {title !== "" && <h1 className="j-doc-title">{title}</h1>}
      {lede !== "" && <p className={ledeClass}>{lede}</p>}
      {sub}
      {tiles.length > 0 && <Tiles tiles={tiles} />}
      {children}
    </header>
  );
}

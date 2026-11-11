/**
 * A text as a document of blocks, one element a paragraph, a heading, a list
 * item, a quote, a code block or a table.
 *
 * The server cuts the text (`viewer_blocks.blocks`) and sends each block with
 * its Markdown and the lines of the text it holds, so a block is a *unit* of
 * the comment gutter: it carries its first line and its last line, the "+"
 * writes a comment on its first line, and the open comments on any of its
 * lines show under it. A list item is one block in its own list, and a nested
 * item carries the depth that indents it.
 */

import { Unit, type Gutter, type Span } from "../comments/Gutter";
import Markdown from "../md/Markdown";

/** One block, as `GET /api/v1/documents/<owner>/<slug>` sends it. */
export interface DocBlock {
  id: string;
  kind: string;
  depth: number;
  markdown: string;
  first_line: number;
  last_line: number;
}

/** What each kind of block is called, the table of `viewer_showcase`. */
export const BLOCK_NAMES: Record<string, string> = {
  p: "paragraph",
  h: "heading",
  li: "list item",
  quote: "quote",
  code: "code block",
  table: "table",
};

/** The lines of one block, as the gutter reads a unit. */
export function span(block: DocBlock): Span {
  return { first: block.first_line, last: block.last_line };
}

/** The units of a document of blocks, in the order of the page. */
export function spans(blocks: DocBlock[]): Span[] {
  return blocks.map(span);
}

/** What a comment on this block is on: `the paragraph at line 3`. */
export function what(block: DocBlock): string {
  return `the ${BLOCK_NAMES[block.kind] ?? "block"} at line ${block.first_line}`;
}

export default function Blocks({ blocks, gutter }: { blocks: DocBlock[]; gutter: Gutter }) {
  return (
    <div className="a-blocks d-body">
      {blocks.map((block) => (
        <Unit
          key={block.id}
          gutter={gutter}
          unit={span(block)}
          className={`a-block ${block.kind} d${block.depth}`}
          what={what(block)}
        >
          <Markdown text={block.markdown} />
        </Unit>
      ))}
    </div>
  );
}

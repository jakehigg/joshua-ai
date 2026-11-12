// The fenced blocks that an answer carries beside its text.
//
// A turn that wrote a document ends with one fenced code block whose info
// string is `artifact`, holding the document path alone (see
// ArtifactCard.tsx):
//
//   ```artifact
//   documents/jake/plan.md
//   ```
//
// An old answer can also end with a `speech` block, the short spoken version
// that voice used to ask for. Voice is off, but a stored answer keeps its
// block, so the block is taken out of the printed answer the same way.
//
// splitFence() takes the blocks of one info string out. `content` is the
// content of the last such block (trimmed; an empty block is null). Every
// block of that info string is removed from `display`, with the blank lines
// around it, so the printed answer never shows one as code and has no empty
// tail. A `~~~` fence works too, and a block with no closing fence at the
// end takes the rest of the text. Such a fence inside another code block is
// code, not a block. With no block, `display` is the whole text and
// `content` is null.

export interface FencedBlock {
  display: string;
  content: string | null;
}

export const SPEECH_INFO = "speech";
export const ARTIFACT_INFO = "artifact";

const OPEN = /^\s{0,3}(`{3,}|~{3,})(.*)$/;
const CLOSE = /^\s{0,3}(`{3,}|~{3,})\s*$/;

// The fence marker and info string of an opening fence line, or null.
export function fenceOpen(line: string): { marker: string; info: string } | null {
  const m = line.match(OPEN);
  if (!m) return null;
  const marker = m[1] as string;
  const info = (m[2] ?? "").trim();
  // A backtick fence cannot have a backtick in its info string.
  if (marker[0] === "`" && info.includes("`")) return null;
  return { marker, info };
}

export function closesFence(line: string, marker: string): boolean {
  const m = line.match(CLOSE);
  return !!m && m[1]?.[0] === marker[0] && (m[1]?.length ?? 0) >= marker.length;
}

// True when the info string of a fence is `want` (an all-lowercase name).
export function isInfo(info: string, want: string): boolean {
  return info.trim().toLowerCase() === want;
}

interface Block {
  start: number;
  end: number; // the last line of the block, inclusive
  content: string;
}

// `streaming`: the text is still arriving. A last line that can still grow
// into a fence of `want` (such as "```spe") is then removed from `display`
// too, so it does not show as code for a moment.
export function splitFence(markdown: string, want: string, streaming = false): FencedBlock {
  const lines = markdown.split("\n");
  const blocks: Block[] = [];
  const tail = lines.length - 1;
  // The info string of a fence that opens on the last line, if one does.
  let tailInfo: string | null = null;
  for (let i = 0; i < lines.length; i++) {
    const open = fenceOpen(lines[i] as string);
    if (!open) continue;
    if (i === tail) tailInfo = open.info;
    let end = i + 1;
    while (end < lines.length && !closesFence(lines[end] as string, open.marker)) end++;
    const last = Math.min(end, lines.length - 1);
    if (isInfo(open.info, want)) {
      blocks.push({ start: i, end: last, content: lines.slice(i + 1, end).join("\n") });
    }
    i = last;
  }
  if (streaming && tailInfo !== null && !isInfo(tailInfo, want) && want.startsWith(tailInfo.toLowerCase())) {
    blocks.push({ start: tail, end: tail, content: "" });
  }
  if (blocks.length === 0) return { display: markdown, content: null };

  const out: string[] = [];
  let gap = false;
  let next = 0;
  const blank = (line: string) => !line.trim();
  for (let i = 0; i < lines.length; i++) {
    const block = blocks[next];
    if (block && i === block.start) {
      while (out.length && blank(out[out.length - 1] as string)) out.pop();
      gap = true;
      i = block.end;
      next++;
      continue;
    }
    const line = lines[i] as string;
    if (gap) {
      if (blank(line)) continue;
      if (out.length) out.push("");
      gap = false;
    }
    out.push(line);
  }
  while (out.length && blank(out[out.length - 1] as string)) out.pop();
  const content = (blocks[blocks.length - 1] as Block).content.trim();
  return { display: out.join("\n"), content: content || null };
}

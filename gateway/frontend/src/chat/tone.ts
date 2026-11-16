// The bubble colour of one writer in a shared thread.
//
// The transcript already names who wrote each turn (`meta.speaker`, which the
// history route gives as `who`), and that name is the only key here: the same
// writer keeps the same colour through the thread, in every session, and on
// every device, because nothing is stored and nothing is counted. Two writers
// who share a display name share a colour, and a renamed person takes a new
// one. The key is one argument, so a person id takes its place the day a
// transcript row carries one.

/** How many bubble colours index.css defines: `.tone-0` to `.tone-5`. */
export const TONES = 6;

/** The tone class of a writer, from the name the transcript gives. */
export function toneOf(who: string): string {
  let hash = 0;
  for (let i = 0; i < who.length; i += 1) {
    hash = (hash * 31 + who.charCodeAt(i)) % 1000003;
  }
  return `tone-${hash % TONES}`;
}

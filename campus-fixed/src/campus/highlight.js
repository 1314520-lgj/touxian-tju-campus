/**
 * Mark the parts of a search result that came from the query.
 *
 * A hit already carries the paragraph it matched, but not *where* inside it the
 * query landed. Marking that is what turns a wall of text into an answer the
 * reader can check at a glance, and it matches how the rest of the product
 * behaves: show the evidence, do not ask the reader to trust a summary.
 *
 * Two constraints shape the rule.
 *
 * * The excerpt is registered as evidence and verified by substring, so the
 *   marked runs must concatenate back to exactly the original text. Nothing
 *   here rewrites, trims or reorders a character.
 * * Chinese has no word boundaries, and the server matches a query by its
 *   substrings, not by whole words. A reader who typed "奖学金申请材料" must
 *   still see the "奖学金" and the "材料" inside a paragraph. So a run is
 *   marked when it is two or more characters long and appears *anywhere* in the
 *   query — the same substring relation the server ranks by.
 */

const MIN_RUN = 2;   // one character is too weak a signal to be worth marking
const MAX_RUN = 24;  // caps the scan; longer overlaps are marked in one run

function longestRun(source, query, at) {
  const limit = Math.min(MAX_RUN, source.length - at);
  for (let length = limit; length >= MIN_RUN; length -= 1) {
    if (query.includes(source.slice(at, at + length))) return length;
  }
  return 0;
}

export function highlightRuns(text, query) {
  const source = String(text ?? '');
  const needle = String(query ?? '').trim();
  const unmarked = [{ text: source, match: false }];
  if (!source || !needle) return unmarked;
  const lowerSource = source.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  // Case folding can change a string's length for a few scripts. Index
  // arithmetic would then mark the wrong characters, so show plain text
  // instead of a confidently wrong highlight.
  if (lowerSource.length !== source.length || lowerNeedle.length !== needle.length) return unmarked;

  const runs = [];
  const push = (chunk, match) => {
    const last = runs[runs.length - 1];
    // Merge neighbours of the same kind so a long overlap is one mark, not a
    // row of abutting ones that can show a seam.
    if (last && last.match === match) { last.text += chunk; return; }
    runs.push({ text: chunk, match });
  };

  let plain = '';
  let index = 0;
  while (index < source.length) {
    const run = longestRun(lowerSource, lowerNeedle, index);
    if (!run) { plain += source[index]; index += 1; continue; }
    if (plain) { push(plain, false); plain = ''; }
    push(source.slice(index, index + run), true);
    index += run;
  }
  if (plain) push(plain, false);
  return runs;
}

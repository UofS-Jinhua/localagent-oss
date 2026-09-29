// The matching behind the quick open and the command palette: the query's characters in order,
// not necessarily next to each other ("chdr" finds ChatHeader.tsx), best matches first.

export interface Match {
  score: number;
  /** Indices of the matched characters in the target, for highlighting. */
  positions: number[];
}

const BOUNDARY = /[\\/._\-\s:]/;

export function fuzzy(query: string, target: string): Match | null {
  const q = query.replace(/\s+/g, "").toLowerCase();
  if (!q) return { score: 0, positions: [] };
  const t = target.toLowerCase();
  const positions: number[] = [];
  let score = 0;
  let from = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, from);
    if (found < 0) return null;
    const previous = positions[positions.length - 1];
    score += found === previous + 1 ? 6 : 1; // runs of characters count the most
    if (found === 0 || BOUNDARY.test(target[found - 1]) || (target[found] !== t[found] && target[found - 1] === t[found - 1])) {
      score += 4; // the start of a word, a path segment or a camelCase hump
    }
    positions.push(found);
    from = found + 1;
  }
  if (t.startsWith(q)) score += 8;
  return { score: score - target.length * 0.02, positions };
}

/** A path matched on its file name first (what people type), else on the whole path. */
export function fuzzyPath(query: string, path: string): Match | null {
  const slash = path.lastIndexOf("/");
  const name = path.slice(slash + 1);
  const onName = fuzzy(query, name);
  if (onName) return { score: onName.score + 20, positions: onName.positions.map((p) => p + slash + 1) };
  return fuzzy(query, path);
}

// How the suggesters narrow a list as a name is typed: the names that hold what was typed, spaces aside, those that start
// with it first. Not CodeMirror's fuzzy match, which pairs letters in order across a whole name: `#onyx` offered
// "professional-services--advisory-exposure", and Enter, typed to end the line, took it. A name nothing holds shows
// no list, so Enter is a new line.
export function narrow<T>(items: T[], typed: string, name: (item: T) => string): T[] {
  const key = (s: string) => s.toLowerCase().replace(/\s+/g, "");
  const q = key(typed);
  if (!q) return items;
  const starts: T[] = [], holds: T[] = [];
  for (const item of items) {
    const n = key(name(item));
    if (n.startsWith(q)) starts.push(item);
    else if (n.includes(q)) holds.push(item);
  }
  return [...starts, ...holds];
}

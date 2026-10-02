/**
 * Obsidian keeps its colour mode as a class on <body>, `theme-light` or `theme-dark`, and every theme keys its
 * variables on it. Onyx can wear either of a vault's modes (its Color theme setting), so the plugin measures the one
 * Obsidian isn't showing too: it flips the class, reads, and flips it back, all in one synchronous run. Nothing paints
 * in between, so the window never shows the other mode; only the computed styles do. (A nested `.theme-dark` wrapper
 * can't stand in: Obsidian declares its semantic variables on body from the mode's base colours, and a child inherits
 * them already resolved.)
 */

/** Runs `read` with the body in its other colour mode, then puts the class back. Undefined without a mode class. */
export function inOtherMode<T>(body: { classList: DOMTokenList }, read: () => T): T | undefined {
  const list = body.classList;
  const from = list.contains("theme-dark") ? "theme-dark" : list.contains("theme-light") ? "theme-light" : "";
  if (!from) return undefined;
  const to = from === "theme-dark" ? "theme-light" : "theme-dark";
  // replace() keeps the class's place, so the body's class attribute reads exactly as before once it is back.
  list.replace(from, to);
  try {
    return read();
  } finally {
    list.replace(to, from);
  }
}

/** What the body-change observer compares: a flip that ends where it began is not a change worth a sync. */
export function bodySignature(body: { className: string; getAttribute(name: string): string | null }): string {
  return `${body.className}\n${body.getAttribute("style") ?? ""}`;
}

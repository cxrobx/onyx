// Folding, as Obsidian's two defaults ("Fold heading", "Fold indent") have it: a heading folds its section, up to the
// next heading as high or higher, and a list item folds what sits under it. The arrow sits in the margin to the left of
// the text, shown faintly while the pointer is over the note, fully over the arrow, and always on a folded line; a
// folded line ends in "…", and a click on either opens it. CodeMirror's Markdown parser would also fold code blocks
// and quotes, which Obsidian doesn't, so those are turned off where the language is built (main.ts).
import { codeFolding, ensureSyntaxTree, foldGutter, foldKeymap, foldService, syntaxTree } from "@codemirror/language";
import { EditorState, Extension } from "@codemirror/state";
import { keymap } from "@codemirror/view";

const HEADING = /^(?:ATX|Setext)Heading(\d)$/;

function headingLevel(state: EditorState, lineStart: number, lineEnd: number): number {
  const tree = syntaxTree(state);
  for (let node: { name: string; from: number; parent: unknown } | null = tree.resolveInner(lineEnd, -1); node; node = node.parent as typeof node) {
    const m = HEADING.exec(node.name);
    if (m) return node.from >= lineStart ? +m[1] : 0;
  }
  return 0;
}

// The end of a run, before the blank lines that trail it, so a fold leaves the gap before what follows.
function trimmed(state: EditorState, from: number, to: number): number {
  let end = to;
  while (end > from) {
    const line = state.doc.lineAt(end);
    if (line.text.trim() || line.from <= from) break;
    end = line.from - 1;
  }
  return Math.max(from, end);
}

function section(state: EditorState, lineStart: number, lineEnd: number) {
  const level = headingLevel(state, lineStart, lineEnd);
  if (!level) return null;
  const tree = ensureSyntaxTree(state, state.doc.length, 50) ?? syntaxTree(state);
  let to = state.doc.length;
  tree.iterate({
    from: lineEnd,
    enter(node) {
      if (to !== state.doc.length) return false;
      const m = HEADING.exec(node.name);
      if (m && node.from > lineEnd && +m[1] <= level) { to = node.from - 1; return false; }
      return undefined;
    },
  });
  const end = trimmed(state, lineEnd, to);
  return end > lineEnd ? { from: lineEnd, to: end } : null;
}

function listItem(state: EditorState, lineStart: number, lineEnd: number) {
  const tree = syntaxTree(state);
  const text = state.doc.sliceString(lineStart, lineEnd), lead = /^[\s>]*/.exec(text)![0].length;
  for (let node: { name: string; from: number; to: number; parent: unknown } | null = tree.resolveInner(lineStart + lead, 1); node; node = node.parent as typeof node) {
    if (node.name !== "ListItem") continue;
    if (node.from < lineStart || node.from > lineEnd) return null;  // a later line of an item, not its first
    const end = trimmed(state, lineEnd, node.to);
    return end > lineEnd ? { from: lineEnd, to: end } : null;
  }
  return null;
}

const CHEVRON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>';

export const folding: Extension = [
  foldService.of((state, lineStart, lineEnd) => section(state, lineStart, lineEnd) ?? listItem(state, lineStart, lineEnd)),
  codeFolding({
    placeholderDOM(_view, onclick) {
      const el = document.createElement("span");
      el.className = "askw-ed-folded";
      el.textContent = "…";
      el.title = "Unfold";
      // On the press: WebKit's click comes after the editor has redrawn the line, and lands on a fresh "…".
      el.addEventListener("mousedown", (e) => { e.preventDefault(); onclick(e); });
      return el;
    },
  }),
  foldGutter({
    markerDOM(open) {
      const el = document.createElement("span");
      el.className = "askw-ed-fold" + (open ? "" : " is-folded");
      el.innerHTML = CHEVRON;  // fixed markup
      el.title = open ? "Fold" : "Unfold";
      return el;
    },
  }),
  keymap.of(foldKeymap),
];

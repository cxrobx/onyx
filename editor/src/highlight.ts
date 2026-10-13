// Obsidian's highlight colours (1.14): a colour emoji first inside the marks, `==🔴text==`, colours the highlight and
// isn't shown (viewer.py, `_HIGHLIGHT_COLOURS`). The colours, the `==` picker, and setting a selection's colour.
import { Completion, CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { syntaxTree } from "@codemirror/language";
import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

export const COLOURS: [string, string, string][] = [
  ["red", "Red", "🔴"], ["orange", "Orange", "🟠"], ["yellow", "Yellow", "🟡"],
  ["green", "Green", "🟢"], ["blue", "Blue", "🔵"], ["purple", "Purple", "🟣"],
];
const BY_EMOJI: Record<string, string> = {
  "🔴": "red", "🟥": "red", "🟠": "orange", "🟧": "orange", "🟡": "yellow", "🟨": "yellow",
  "🟢": "green", "🟩": "green", "🔵": "blue", "🟦": "blue", "🟣": "purple", "🟪": "purple",
};
/** The colour an emoji at `pos` names, and its length in the text (an emoji is two UTF-16 units). */
export function colourAt(state: EditorState, pos: number): { colour: string; length: number } | null {
  const ch = state.sliceDoc(pos, pos + 2);
  return BY_EMOJI[ch] ? { colour: BY_EMOJI[ch], length: ch.length } : null;
}

// Typing an opening `==` offers the colours, as Obsidian does: Default (no emoji) or one of six. A closing `==`, or
// one in code, offers nothing.
export function colourSource(context: CompletionContext): CompletionResult | null {
  const typed = context.matchBefore(/==$/);
  if (!typed) return null;
  const line = context.state.doc.lineAt(typed.from), before = line.text.slice(0, typed.from - line.from);
  if ((before.match(/==/g) || []).length % 2) return null;  // this one closes a highlight
  for (let node = syntaxTree(context.state).resolveInner(typed.from, -1) as { name: string; parent: unknown } | null; node; node = node.parent as typeof node) {
    if (/Code|Frontmatter|URL/.test(node.name)) return null;
  }
  const pick = (emoji: string) => (view: EditorView, _c: Completion, from: number, to: number) => {
    view.dispatch({ changes: { from, to, insert: emoji }, selection: { anchor: from + emoji.length }, userEvent: "input.complete" });
  };
  return {
    from: typed.to,
    filter: false,
    options: [
      { label: "Default", swatch: "", apply: pick("") } as Completion,
      ...COLOURS.map(([colour, label, emoji]) => ({ label, swatch: colour, apply: pick(emoji) }) as Completion),
    ],
  };
}

/** The swatch beside each colour in the list. */
export function swatchFor(completion: Completion): Node | null {
  const colour = (completion as Completion & { swatch?: string }).swatch;
  if (colour === undefined) return null;
  const el = document.createElement("span");
  el.className = "askw-ed-swatch";
  if (colour) el.dataset.highlight = colour;
  return el;
}

// The selection highlighted in a colour ("" for the default): an existing highlight around it changes colour, and
// anything else is wrapped.
export function setHighlight(view: EditorView, colour: string) {
  const { state } = view, emoji = COLOURS.find((c) => c[0] === colour)?.[2] ?? "";
  view.dispatch(state.update(state.changeByRange((range) => {
    const after = state.sliceDoc(range.to, range.to + 2) === "==";
    if (after && state.sliceDoc(range.from - 2, range.from) === "==") {
      return { changes: { from: range.from, insert: emoji }, range: EditorSelection.range(range.from + emoji.length, range.to + emoji.length) };
    }
    const old = colourAt(state, range.from - 2);
    if (after && old && state.sliceDoc(range.from - 4, range.from - 2) === "==") {
      const delta = emoji.length - old.length;
      return { changes: { from: range.from - old.length, to: range.from, insert: emoji }, range: EditorSelection.range(range.from + delta, range.to + delta) };
    }
    const open = "==" + emoji;
    return { changes: [{ from: range.from, insert: open }, { from: range.to, insert: "==" }], range: EditorSelection.range(range.from + open.length, range.to + open.length) };
  }), { userEvent: "input", scrollIntoView: true }));
}

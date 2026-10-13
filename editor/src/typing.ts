// What typing does in the editor, as Obsidian does it with its default settings (Auto-pair brackets, Auto-pair Markdown
// syntax, Smart lists, Indent using tabs) and its default hotkeys. main.ts adds these ahead of CodeMirror's own keymap.
import { defaultKeymap, deleteLine, indentWithTab } from "@codemirror/commands";
import { indentUnit } from "@codemirror/language";
import { insertNewlineContinueMarkupCommand, pasteURLAsLink } from "@codemirror/lang-markdown";
import { EditorSelection, EditorState, Extension, Prec, StateCommand } from "@codemirror/state";
import { drawSelection, EditorView, keymap, rectangularSelection } from "@codemirror/view";

// ⌘B and ⌘I, as Obsidian binds them: wrap the selection, or unwrap it when it is already wrapped.
export function toggleWrap(mark: string): StateCommand {
  return ({ state, dispatch }) => {
    const n = mark.length;
    dispatch(state.update(state.changeByRange((range) => {
      const before = state.sliceDoc(range.from - n, range.from), after = state.sliceDoc(range.to, range.to + n);
      if (before === mark && after === mark) {
        return {
          changes: [{ from: range.from - n, to: range.from }, { from: range.to, to: range.to + n }],
          range: EditorSelection.range(range.from - n, range.to - n),
        };
      }
      return {
        changes: [{ from: range.from, insert: mark }, { from: range.to, insert: mark }],
        range: EditorSelection.range(range.from + n, range.to + n),
      };
    }), { userEvent: "input", scrollIntoView: true }));
    return true;
  };
}

// Auto-pair: an opening character typed where nothing but space or a closer follows gets its closer, and a closer typed
// in front of the same closer steps over it. A character typed over a selection wraps it and keeps it selected, so `=`
// twice makes `==highlight==`. The Markdown marks pair only where they can open a span: not inside a word (an apostrophe,
// snake_case), and not at the start of a line, where `*` and `_` begin a bullet or a rule.
const PAIRS: Record<string, string> = { "(": ")", "[": "]", "{": "}", "'": "'", '"': '"', "*": "*", "_": "_", "`": "`" };
const WRAP_ONLY: Record<string, string> = { "=": "=", "~": "~", "$": "$", "%": "%" };
const STEP_OVER = new Set([")", "]", "}", "'", '"', "*", "_", "`"]);
const MARKS = new Set(["*", "_", "`"]);
const PAIR_BEFORE = /^(?:$|[\s)\]}'":;>])/;
const WORD = /[\p{L}\p{N}]/u;

function autoPair(view: EditorView, text: string): boolean {
  const { state } = view;
  if (state.readOnly || view.composing || text.length !== 1) return false;
  const close = PAIRS[text] ?? WRAP_ONLY[text];
  if (!close && !STEP_OVER.has(text)) return false;
  let handled = false;
  const tr = state.changeByRange((range) => {
    if (!range.empty) {
      if (!close) return { range };
      handled = true;
      return {
        changes: [{ from: range.from, insert: text }, { from: range.to, insert: close }],
        range: EditorSelection.range(range.anchor + 1, range.head + 1),
      };
    }
    const pos = range.head, line = state.doc.lineAt(pos);
    const prev = state.sliceDoc(pos - 1, pos), next = state.sliceDoc(pos, pos + 1), lineBefore = state.sliceDoc(line.from, pos);
    // Inside an empty pair of marks, another one nests: `*|*` then `*` is `**|**`. Closing one (`**b*|*`) steps over.
    if (MARKS.has(text) && prev === text && next === text && !WORD.test(lineBefore.replace(/[*_`]+$/, "").slice(-1))) {
      handled = true;
      return { changes: { from: pos, insert: text + text }, range: EditorSelection.cursor(pos + 1) };
    }
    if (STEP_OVER.has(text) && next === text) {
      handled = true;
      return { range: EditorSelection.cursor(pos + 1) };
    }
    if (!PAIRS[text] || !PAIR_BEFORE.test(state.sliceDoc(pos, line.to))) return { range };
    if ((text === "'" || text === '"' || MARKS.has(text)) && WORD.test(prev)) return { range };
    if ((text === "*" || text === "_") && /^\s*(?:>\s*)*[*_]*$/.test(lineBefore)) return { range };
    handled = true;
    return { changes: { from: pos, insert: text + close }, range: EditorSelection.cursor(pos + 1) };
  });
  if (!handled) return false;
  view.dispatch(state.update(tr, { userEvent: "input.type", scrollIntoView: true }));
  return true;
}

// Backspace between an empty pair takes both.
const deletePair: StateCommand = ({ state, dispatch }) => {
  let handled = false;
  const tr = state.changeByRange((range) => {
    if (!range.empty) return { range };
    const pos = range.head, prev = state.sliceDoc(pos - 1, pos), next = state.sliceDoc(pos, pos + 1);
    if (!prev || (PAIRS[prev] ?? WRAP_ONLY[prev]) !== next) return { range };
    handled = true;
    return { changes: { from: pos - 1, to: pos + 1 }, range: EditorSelection.cursor(pos - 1) };
  });
  if (!handled) return false;
  dispatch(state.update(tr, { userEvent: "delete.backward", scrollIntoView: true }));
  return true;
};

// The quote marks and list marker a line starts with, if any.
export const LEAD = /^(\s*(?:>\s?)*)(?:([-*+]|\d+[.)])([ \t]+)(?:\[(.)\][ \t]?)?)?/;

// ⌘L, Obsidian's "Toggle checkbox status", on every line the selection touches: a task is ticked or unticked, a list
// item becomes a task, and any other line a task item.
export const toggleCheckbox: StateCommand = ({ state, dispatch }) => {
  const changes: { from: number; to?: number; insert: string }[] = [], seen = new Set<number>();
  for (const range of state.selection.ranges) {
    for (let n = state.doc.lineAt(range.from).number; n <= state.doc.lineAt(range.to).number; n++) {
      if (seen.has(n)) continue;
      seen.add(n);
      const line = state.doc.line(n), m = LEAD.exec(line.text)!;
      const [whole, quote, marker, gap, box] = m;
      if (box !== undefined) {
        const at = line.from + whole.lastIndexOf("[") + 1;
        changes.push({ from: at, to: at + 1, insert: box === " " ? "x" : " " });
      } else if (marker) {
        changes.push({ from: line.from + quote.length + marker.length + gap.length, insert: "[ ] " });
      } else {
        changes.push({ from: line.from + quote.length, insert: "- [ ] " });
      }
    }
  }
  const set = state.changes(changes);
  dispatch(state.update({ changes: set, selection: state.selection.map(set, 1), userEvent: "input", scrollIntoView: true }));
  return true;
};

// ⌘K, Obsidian's "Insert Markdown link": `[selection]()` with the cursor in the parentheses, or `[]()` with it in the
// brackets.
export const insertLink: StateCommand = ({ state, dispatch }) => {
  dispatch(state.update(state.changeByRange((range) => {
    const label = state.sliceDoc(range.from, range.to);
    return {
      changes: { from: range.from, to: range.to, insert: `[${label}]()` },
      range: EditorSelection.cursor(range.empty ? range.from + 1 : range.from + label.length + 3),
    };
  }), { userEvent: "input", scrollIntoView: true }));
  return true;
};

// ⌘/, Obsidian's "Toggle comment": `%%…%%` around the selection, or around the line's text when nothing is selected.
export const toggleComment: StateCommand = (target) => {
  const { state, dispatch } = target;
  if (state.selection.ranges.some((r) => !r.empty)) return toggleWrap("%%")(target);
  dispatch(state.update(state.changeByRange((range) => {
    const line = state.doc.lineAt(range.head), lead = LEAD.exec(line.text)![0], from = line.from + lead.length;
    const body = state.sliceDoc(from, line.to), m = /^%%([\s\S]*)%%$/.exec(body);
    if (m) return { changes: { from, to: line.to, insert: m[1] }, range: EditorSelection.cursor(Math.max(from, range.head - 2)) };
    return { changes: [{ from, insert: "%%" }, { from: line.to, insert: "%%" }], range: EditorSelection.cursor(range.head + 2) };
  }), { userEvent: "input", scrollIntoView: true }));
  return true;
};

// Shift+Enter: a new line in the same list item or quote, lined up under its text, with no new marker.
const softBreak: StateCommand = ({ state, dispatch }) => {
  dispatch(state.update(state.changeByRange((range) => {
    const line = state.doc.lineAt(range.from), [whole, quote, marker] = LEAD.exec(line.text)!;
    // On an item's first line, its marker (and task box) turns to spaces; on a line already inside one, its indent stays.
    const lead = marker
      ? quote + whole.slice(quote.length).replace(/\[.\][ \t]?$/, "").replace(/[^\t]/g, " ")
      : /^[\s>]*/.exec(line.text)![0];
    const insert = state.lineBreak + lead;
    return { changes: { from: range.from, to: range.to, insert }, range: EditorSelection.cursor(range.from + insert.length) };
  }), { userEvent: "input", scrollIntoView: true }));
  return true;
};

const continueMarkup = insertNewlineContinueMarkupCommand({ nonTightLists: false });
// Enter on a quote line with nothing in it takes one level of quote away, so a second Enter leaves the quote, where
// CodeMirror would want two empty quote lines first.
const leaveQuote: StateCommand = ({ state, dispatch }) => {
  const main = state.selection.main;
  if (state.selection.ranges.length > 1 || !main.empty) return false;
  const line = state.doc.lineAt(main.head);
  if (main.head !== line.to || !/^\s*(?:>\s?)+$/.test(line.text)) return false;
  const keep = line.text.replace(/>\s?$/, "");
  dispatch(state.update({ changes: { from: line.from, to: line.to, insert: keep }, selection: { anchor: line.from + keep.length }, userEvent: "delete", scrollIntoView: true }));
  return true;
};

// CodeMirror's Option+↑/↓ move and copy lines; Obsidian leaves those keys to the Mac, which moves the cursor.
const MOVE_LINE = new Set(["Alt-ArrowUp", "Alt-ArrowDown", "Shift-Alt-ArrowUp", "Shift-Alt-ArrowDown"]);

export const typing: Extension = [
  indentUnit.of("\t"),
  EditorState.allowMultipleSelections.of(true),
  drawSelection(),
  // Option-click adds a cursor and Shift-Option-drag selects a rectangle, as in Obsidian. (⌘-click follows a link.)
  EditorView.clickAddsSelectionRange.of((event) => event.altKey && !event.shiftKey),
  rectangularSelection({ eventFilter: (event) => event.altKey && event.shiftKey }),
  EditorView.inputHandler.of((view, _from, _to, text) => autoPair(view, text)),
  pasteURLAsLink,
  // Enter on an empty item always ends it, or takes it out a level when it is nested, where CodeMirror would rather turn
  // a two-item list loose.
  Prec.highest(keymap.of([
    { key: "Enter", run: (t) => leaveQuote(t) || continueMarkup(t) },
    { key: "Backspace", run: deletePair },
  ])),
  keymap.of([
    { key: "Mod-b", run: toggleWrap("**") },
    { key: "Mod-i", run: toggleWrap("*") },
    { key: "Mod-l", run: toggleCheckbox, preventDefault: true },
    { key: "Mod-k", run: insertLink, preventDefault: true },
    { key: "Mod-/", run: toggleComment },
    { key: "Mod-d", run: deleteLine, preventDefault: true },
    { key: "Shift-Enter", run: softBreak },
    indentWithTab,
    ...defaultKeymap.filter((b) => !MOVE_LINE.has(b.key ?? "")),
  ]),
];

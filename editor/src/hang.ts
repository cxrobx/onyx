// A list item hangs, as Obsidian draws it: its wrapped lines, and the lines of it after the first (Shift+Enter's), start
// under its text rather than back under its bullet. How far in that is depends on the indent, the marker and the task
// box as drawn, so it is measured, not counted: each item line's lead, and each continuation line's own indent, which
// it takes back so its text lines up with its item's. A line's leading tabs are boxed: a tab is drawn to the next tab stop,
// so one shifted by the hang would draw a different width than was measured.
import { syntaxTree } from "@codemirror/language";
import { Range, StateEffect, StateField, Text } from "@codemirror/state";
import { Decoration, DecorationSet, EditorView, ViewPlugin, ViewUpdate } from "@codemirror/view";

import { LEAD } from "./typing";

const setHangs = StateEffect.define<DecorationSet>();

const hangs = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(deco, tr) {
    for (const e of tr.effects) if (e.is(setHangs)) return e.value;
    return deco.map(tr.changes);
  },
  provide: (f) => EditorView.decorations.from(f),
});

// Inside one of these a line keeps its own indent: it is code, or a table row.
const OWN_INDENT = new Set(["FencedCode", "CodeBlock", "Table", "HTMLBlock"]);

const indentBox = Decoration.mark({ class: "askw-ed-indent" });

function width(view: EditorView, from: number, to: number): number | null {
  if (from === to) return 0;
  const a = view.coordsAtPos(from, 1), b = view.coordsAtPos(to, -1);
  return a && b ? Math.max(0, Math.round(b.left - a.left)) : null;
}

function measure(view: EditorView): { key: string; deco: Range<Decoration>[]; doc: Text } {
  const { state } = view, tree = syntaxTree(state), doc = state.doc;
  const deco: Range<Decoration>[] = [], parts: string[] = [], itemHang = new Map<number, number | null>();
  for (const { from, to } of view.visibleRanges) {
    for (let pos = from; pos <= to;) {
      const line = doc.lineAt(pos);
      pos = line.to + 1;
      // A quote's lines have their own padding and rule; a list in one is left as it was.
      if (!line.text.trim() || /^\s*>/.test(line.text)) continue;
      const ws = /^[ \t]*/.exec(line.text)![0].length;
      let item = null;
      for (let n: ReturnType<typeof tree.resolveInner> | null = tree.resolveInner(line.from + ws, 1); n; n = n.parent) {
        if (OWN_INDENT.has(n.name)) break;
        if (n.name === "ListItem") { item = n; break; }
      }
      if (!item) continue;
      const first = doc.lineAt(item.from);
      if (!itemHang.has(first.from)) itemHang.set(first.from, width(view, first.from, first.from + LEAD.exec(first.text)![0].length));
      const hang = itemHang.get(first.from), lead = first.number === line.number ? hang : width(view, line.from, line.from + ws);
      if (hang == null || lead == null || (!hang && !lead)) continue;
      deco.push(Decoration.line({ class: "askw-ed-hang", attributes: { style: `--askw-hang:${hang}px;--askw-lead:${lead}px` } }).range(line.from));
      if (ws) deco.push(indentBox.range(line.from, line.from + ws));
      parts.push(`${line.from}:${hang}:${lead}`);
    }
  }
  return { key: parts.join(","), deco, doc };
}

export const hangingIndent = [
  hangs,
  ViewPlugin.fromClass(class {
    key = "";
    done = false;
    constructor(readonly view: EditorView) { this.schedule(); }
    update(u: ViewUpdate) {
      if (u.docChanged || u.viewportChanged || u.geometryChanged || u.selectionSet || syntaxTree(u.startState) !== syntaxTree(u.state)) this.schedule();
    }
    schedule() {
      this.view.requestMeasure({
        key: this,
        read: (view) => measure(view),
        // A measure's write runs mid-update, where a dispatch isn't allowed; a microtask still lands before the paint.
        // A note changed in between is measured again, as that change asked.
        write: ({ key, deco, doc }, view) => {
          if (key === this.key) return;
          queueMicrotask(() => {
            if (this.done || view.state.doc !== doc) return;
            this.key = key;
            view.dispatch({ effects: setHangs.of(Decoration.set(deco, true)) });
          });
        },
      });
    }
    destroy() { this.done = true; }
  }),
];

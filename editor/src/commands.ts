// Slash commands, as Obsidian's core plugin offers them: `/` at the start of a line or after a space lists the editor's
// commands, narrowed as their name is typed; Enter runs one in place of what was typed, and Space or Escape leaves the
// `/` as text. suggest.ts adds this source beside its own.
import { Completion, CompletionContext, CompletionResult, startCompletion } from "@codemirror/autocomplete";
import { syntaxTree } from "@codemirror/language";
import { EditorSelection, StateCommand } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

import { narrow } from "./match";
import { insertLink, LEAD, toggleCheckbox, toggleComment, toggleWrap } from "./typing";

// Each line the selection touches, rewritten by `change` (given its text and its place among them), the cursor kept
// where it was in the line's text.
function eachLine(change: (text: string, index: number) => string): StateCommand {
  return ({ state, dispatch }) => {
    const changes: { from: number; to: number; insert: string }[] = [], seen = new Set<number>();
    for (const range of state.selection.ranges) {
      for (let n = state.doc.lineAt(range.from).number; n <= state.doc.lineAt(range.to).number; n++) {
        if (seen.has(n)) continue;
        seen.add(n);
        const line = state.doc.line(n), next = change(line.text, seen.size - 1);
        if (next !== line.text) changes.push({ from: line.from, to: line.to, insert: next });
      }
    }
    if (!changes.length) return true;
    const set = state.changes(changes);
    // A cursor keeps its distance from the end of its line, so a prefix put in or taken out moves it along.
    const selection = EditorSelection.create(state.selection.ranges.map((r) => {
      const keep = (pos: number) => {
        const line = state.doc.lineAt(pos), fromEnd = line.to - pos, mapped = set.mapPos(line.to, 1);
        const start = set.mapPos(line.from, -1);
        return Math.max(start, mapped - fromEnd);
      };
      return EditorSelection.range(keep(r.anchor), keep(r.head));
    }), state.selection.mainIndex);
    dispatch(state.update({ changes: set, selection, userEvent: "input", scrollIntoView: true }));
    return true;
  };
}

// A line's quote marks, then the rest without its list marker or task box.
function split(text: string): { quote: string; indent: string; marker: string; box: string | undefined; rest: string } {
  const m = LEAD.exec(text)!, quoteAndIndent = m[1];
  const quote = /^(?:\s*>\s?)*/.exec(quoteAndIndent)![0];
  return { quote, indent: quoteAndIndent.slice(quote.length), marker: m[2] ?? "", box: m[4], rest: text.slice(m[0].length) };
}
export const heading = (level: number) => eachLine((text) => {
  const [, quote, rest] = /^(\s*(?:>\s?)*)(?:#{1,6}[ \t]+)?(.*)$/.exec(text)!;
  return quote + (level ? "#".repeat(level) + " " : "") + rest;
});
export const bulletList = eachLine((text) => {
  const p = split(text);
  if (p.marker && /[-*+]/.test(p.marker) && p.box === undefined) return p.quote + p.indent + p.rest;
  return p.quote + p.indent + "- " + p.rest;
});
export const numberedList = eachLine((text, i) => {
  const p = split(text);
  if (/^\d/.test(p.marker)) return p.quote + p.indent + p.rest;
  return p.quote + p.indent + `${i + 1}. ` + p.rest;
});
export const checklist = eachLine((text) => {
  const p = split(text);
  if (p.box !== undefined) return p.quote + p.indent + p.rest;
  return p.quote + p.indent + "- [ ] " + p.rest;
});
export const blockquote = eachLine((text) => (/^\s*>\s?/.test(text) ? text.replace(/^(\s*)>\s?/, "$1") : "> " + text));

// The selected lines inside a fenced code block, or a new empty one at the cursor.
export const codeBlock: StateCommand = (target) => {
  const { state, dispatch } = target, range = state.selection.main;
  if (range.empty) return block("```{cursor}\n\n```")(target);
  const first = state.doc.lineAt(range.from), last = state.doc.lineAt(range.to);
  dispatch(state.update({
    changes: [{ from: first.from, insert: "```" + state.lineBreak }, { from: last.to, insert: state.lineBreak + "```" }],
    selection: { anchor: first.from + 3 },
    userEvent: "input",
    scrollIntoView: true,
  }));
  return true;
};

// The inline formatting the selection takes in, its marks taken out: emphasis, bold, code, strikethrough, highlight,
// whole spans whose marks sit just outside the selection included.
const SPANS = /^(Emphasis|StrongEmphasis|InlineCode|Strikethrough|Highlight)$/;
const SPAN_MARKS = /^(EmphasisMark|CodeMark|StrikethroughMark|HighlightMark)$/;
export const clearFormatting: StateCommand = ({ state, dispatch }) => {
  const changes: { from: number; to: number }[] = [], seen = new Set<number>();
  for (const range of state.selection.ranges) {
    if (range.empty) continue;
    syntaxTree(state).iterate({
      from: range.from, to: range.to,
      enter(node) {
        if (!SPANS.test(node.name) || node.to <= range.from || node.from >= range.to) return;
        for (let mark = node.node.firstChild; mark; mark = mark.nextSibling) {
          if (SPAN_MARKS.test(mark.name) && !seen.has(mark.from)) { seen.add(mark.from); changes.push({ from: mark.from, to: mark.to }); }
        }
      },
    });
  }
  if (!changes.length) return false;
  dispatch(state.update({ changes, userEvent: "delete", scrollIntoView: true }));
  return true;
};

// Put a block in at the cursor, on lines of its own; `{cursor}` marks where the cursor goes.
function block(template: string): StateCommand {
  return ({ state, dispatch }) => {
    const range = state.selection.main, line = state.doc.lineAt(range.from);
    const before = line.text.trim() ? state.lineBreak : "", at = line.text.trim() ? line.to : line.from;
    const text = before + template.replace("{cursor}", ""), cursor = at + before.length + template.indexOf("{cursor}");
    dispatch(state.update({
      changes: { from: at, to: line.text.trim() ? at : line.to, insert: text },
      selection: { anchor: cursor },
      userEvent: "input",
      scrollIntoView: true,
    }));
    return true;
  };
}
function insertText(make: () => string): StateCommand {
  return ({ state, dispatch }) => {
    dispatch(state.update(state.replaceSelection(make()), { userEvent: "input", scrollIntoView: true }));
    return true;
  };
}
const pad = (n: number) => String(n).padStart(2, "0");
const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
const now = () => { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

// A StateCommand runs on the view itself, which carries the state and dispatch it asks for.
interface SlashCommand { name: string; key?: string; run: (view: EditorView) => unknown }
const COMMANDS: SlashCommand[] = [
  { name: "Add internal link", run: (view) => { insertText(() => "[[]]")(view); view.dispatch({ selection: { anchor: view.state.selection.main.head - 2 } }); startCompletion(view); } },
  { name: "Insert Markdown link", key: "⌘K", run: insertLink },
  { name: "Toggle bold", key: "⌘B", run: toggleWrap("**") },
  { name: "Toggle italics", key: "⌘I", run: toggleWrap("*") },
  { name: "Toggle strikethrough", run: toggleWrap("~~") },
  { name: "Toggle highlight", run: toggleWrap("==") },
  { name: "Toggle code", run: toggleWrap("`") },
  { name: "Toggle comment", key: "⌘/", run: toggleComment },
  { name: "Toggle bullet list", run: bulletList },
  { name: "Toggle numbered list", run: numberedList },
  { name: "Toggle checklist", run: checklist },
  { name: "Toggle checkbox status", key: "⌘L", run: toggleCheckbox },
  { name: "Toggle blockquote", run: blockquote },
  ...[1, 2, 3, 4, 5, 6].map((n) => ({ name: `Set as heading ${n}`, run: heading(n) })),
  { name: "Remove heading", run: heading(0) },
  { name: "Insert callout", run: block("> [!note]\n> {cursor}") },
  { name: "Insert code block", run: block("```{cursor}\n\n```") },
  { name: "Insert table", run: block("| {cursor} |  |\n| --- | --- |\n|  |  |") },
  { name: "Insert horizontal rule", run: block("---\n{cursor}") },
  { name: "Insert current date", run: insertText(today) },
  { name: "Insert current time", run: insertText(now) },
];

export function slashSource(context: CompletionContext): CompletionResult | null {
  const typed = context.matchBefore(/(?:^|\s)\/[\p{L}\p{N}-]*$/u);
  if (!typed) return null;
  const slash = typed.from + typed.text.indexOf("/");
  for (let node = syntaxTree(context.state).resolveInner(slash, -1) as { name: string; parent: unknown } | null; node; node = node.parent as typeof node) {
    if (/Code|Frontmatter|URL/.test(node.name)) return null;
  }
  const commands = narrow(COMMANDS, typed.text.slice(typed.text.indexOf("/") + 1), (c) => c.name);
  if (!commands.length) return null;
  return {
    from: slash + 1,
    filter: false,
    options: commands.map((c) => ({
      label: c.name,
      detail: c.key,
      apply(view: EditorView, _c: Completion, _from: number, to: number) {
        view.dispatch({ changes: { from: slash, to }, selection: { anchor: slash }, userEvent: "delete" });
        c.run(view);
      },
    })),
  };
}

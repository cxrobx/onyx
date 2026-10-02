// Live Preview, as Obsidian draws it: the note is its own Markdown source, styled as it will read, with the syntax
// (`#`, `**`, `[`…`](url)`, `>`) hidden everywhere except where the cursor is, so what is being edited shows as typed.
// A table is drawn as a table and an image as the picture until the cursor goes into one; a task's box ticks on click.
//
// It follows the reader's renderer (viewer.py: CommonMark, tables, strikethrough, ==highlights==, tasks, wikilinks)
// rather than Obsidian's full dialect, so ⌘E never shows a construct styled one way here and left as plain text on the
// page it toggles back to. Add a construct to both.
import { syntaxTree } from "@codemirror/language";
import { EditorState, Facet, Range, StateEffect, StateField, Text } from "@codemirror/state";
import { Decoration, DecorationSet, EditorView, ViewPlugin, ViewUpdate, WidgetType } from "@codemirror/view";
import type { SyntaxNode } from "@lezer/common";
import type { DelimiterType, MarkdownConfig } from "@lezer/markdown";

// `[[Note]]`, `[[Note|Alias]]`, `[[Note#Heading]]` and `![[embed]]`, with the reader's limits: one line, not empty, no
// nested `[[`. Parsed before Link, as the reader does, so a wikilink is never taken for a reference link.
export const wikilinks: MarkdownConfig = {
  defineNodes: ["WikiLink", "WikiEmbed", "WikiLinkMark", "WikiLinkTarget", "WikiLinkAlias"],
  parseInline: [{
    name: "WikiLink",
    before: "Link",
    parse(cx, next, pos) {
      const embed = next === 33 && cx.char(pos + 1) === 91 && cx.char(pos + 2) === 91;
      if (!embed && !(next === 91 && cx.char(pos + 1) === 91)) return -1;
      const open = pos + (embed ? 3 : 2);
      const close = cx.slice(open, cx.end).indexOf("]]");
      if (close < 0) return -1;
      const inner = cx.slice(open, open + close);
      if (!inner.trim() || inner.includes("\n") || inner.includes("[[")) return -1;
      const end = open + close + 2, pipe = inner.indexOf("|");
      const children = [cx.elt("WikiLinkMark", pos, open)];
      if (pipe >= 0) {
        children.push(
          cx.elt("WikiLinkTarget", open, open + pipe),
          cx.elt("WikiLinkMark", open + pipe, open + pipe + 1),
          cx.elt("WikiLinkAlias", open + pipe + 1, open + close),
        );
      } else {
        children.push(cx.elt("WikiLinkTarget", open, open + close));
      }
      children.push(cx.elt("WikiLinkMark", open + close, end));
      return cx.addElement(cx.elt(embed ? "WikiEmbed" : "WikiLink", pos, end, children));
    },
  }],
};

// `==highlight==`, delimited as @lezer/markdown delimits `~~strikethrough~~` (and as the reader's port of
// markdown-it's does): a run of two `=` that can open where it doesn't sit before a space, close where it doesn't follow one.
const HighlightDelim: DelimiterType = { resolve: "Highlight", mark: "HighlightMark" };
const PUNCTUATION = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~\xA1‐-‧]/;
export const highlights: MarkdownConfig = {
  defineNodes: ["Highlight", "HighlightMark"],
  parseInline: [{
    name: "Highlight",
    after: "Emphasis",
    parse(cx, next, pos) {
      if (next !== 61 || cx.char(pos + 1) !== 61 || cx.char(pos + 2) === 61) return -1;
      const before = cx.slice(pos - 1, pos), after = cx.slice(pos + 2, pos + 3);
      const sBefore = /\s|^$/.test(before), sAfter = /\s|^$/.test(after);
      const pBefore = PUNCTUATION.test(before), pAfter = PUNCTUATION.test(after);
      return cx.addDelimiter(HighlightDelim, pos, pos + 2,
        !sAfter && (!pAfter || sBefore || pBefore), !sBefore && (!pBefore || sAfter || pAfter));
    },
  }],
};

// `%%comment%%` and `#tag`, as Obsidian writes them. The reader hides a comment and draws a tag as a pill
// (viewer.py, `_comment_inline` and `_tag_rule`); here a comment is source to edit, so it is muted, not hidden.
export const comments: MarkdownConfig = {
  defineNodes: ["Comment"],
  parseInline: [{
    name: "Comment",
    before: "InlineCode",
    parse(cx, next, pos) {
      if (next !== 37 || cx.char(pos + 1) !== 37) return -1;
      const close = cx.slice(pos + 2, cx.end).indexOf("%%");
      return close < 0 ? -1 : cx.addElement(cx.elt("Comment", pos, pos + 2 + close + 2));
    },
  }],
};
const TAG = /^#[\p{L}\p{N}_][\p{L}\p{N}_\/-]*/u;  // and not only digits: `#1` is text
export const hashtags: MarkdownConfig = {
  defineNodes: ["Hashtag"],
  parseInline: [{
    name: "Hashtag",
    parse(cx, next, pos) {
      if (next !== 35 || (pos > cx.offset && !/\s/.test(cx.slice(pos - 1, pos)))) return -1;
      const found = TAG.exec(cx.slice(pos, cx.end));
      if (!found || /^[\d\/_-]+$/.test(found[0].slice(1))) return -1;
      return cx.addElement(cx.elt("Hashtag", pos, pos + found[0].length));
    },
  }],
};

// MARK: - What a stretch of inline Markdown reads as, for a table's cells and the outline's headings

export interface Run { t: string; s: string; href?: string; wiki?: string }

const SILENT = new Set(["EmphasisMark", "CodeMark", "StrikethroughMark", "HighlightMark", "LinkMark", "URL", "LinkTitle",
  "LinkLabel", "WikiLinkMark", "HeaderMark", "QuoteMark", "TableDelimiter", "TaskMarker"]);
const STYLE: Record<string, string> = {
  Emphasis: "askw-ed-em", StrongEmphasis: "askw-ed-strong", InlineCode: "askw-ed-code",
  Strikethrough: "askw-ed-strike", Highlight: "askw-ed-highlight", Hashtag: "askw-ed-tag", Comment: "askw-ed-comment",
};

export function inlineRuns(doc: Text, node: SyntaxNode): Run[] {
  const out: Run[] = [];
  const put = (t: string, s: string, extra: Partial<Run> = {}) => { if (t) out.push({ t, s, ...extra }); };
  const walk = (n: SyntaxNode, s: string, extra: Partial<Run>) => {
    let pos = n.from;
    for (let ch = n.firstChild; ch; ch = ch.nextSibling) {
      if (ch.from > pos) put(doc.sliceString(pos, ch.from), s, extra);
      visit(ch, s, extra);
      pos = ch.to;
    }
    if (n.to > pos) put(doc.sliceString(pos, n.to), s, extra);
  };
  const visit = (n: SyntaxNode, s: string, extra: Partial<Run>) => {
    const name = n.name;
    if (name === "URL" && n.parent?.name !== "Link" && n.parent?.name !== "Image" && n.parent?.name !== "Autolink") {
      const href = doc.sliceString(n.from, n.to);
      return put(href, `${s} askw-ed-link`, { href });
    }
    if (SILENT.has(name)) return;
    if (name in STYLE) return walk(n, `${s} ${STYLE[name]}`, extra);
    if (name === "Link") {
      const url = n.getChild("URL");
      return walk(n, `${s} askw-ed-link`, url ? { href: doc.sliceString(url.from, url.to) } : extra);
    }
    if (name === "WikiLink") {
      const target = n.getChild("WikiLinkTarget"), label = n.getChild("WikiLinkAlias") || target;
      if (target && label) put(doc.sliceString(label.from, label.to), `${s} askw-ed-link`, { wiki: wikiTarget(doc, target) });
      return;
    }
    if (name === "Image" || name === "WikiEmbed") return put(doc.sliceString(n.from, n.to), `${s} askw-ed-embed`, extra);
    if (name === "Escape") return put(doc.sliceString(n.from + 1, n.to), s, extra);
    walk(n, s, extra);
  };
  walk(node, "", {});
  return out;
}

// A wikilink's target. In a table its `|` is written `\|`, which leaves the backslash on the target.
function wikiTarget(doc: Text, target: SyntaxNode): string {
  return doc.sliceString(target.from, target.to).replace(/\\$/, "");
}

function runsDOM(runs: Run[], parent: HTMLElement) {
  for (const r of runs) {
    if (!r.s.trim() && !r.href && !r.wiki) { parent.append(r.t); continue; }
    const span = document.createElement("span");
    span.className = r.s.trim();
    span.textContent = r.t;
    if (r.href) span.dataset.askwHref = r.href;
    if (r.wiki) span.dataset.askwWiki = r.wiki;
    parent.append(span);
  }
}

// MARK: - Widgets

class Bullet extends WidgetType {
  eq() { return true; }
  toDOM() {
    const el = document.createElement("span");
    el.className = "askw-ed-bullet";
    el.textContent = "•";
    return el;
  }
}

class Rule extends WidgetType {
  eq() { return true; }
  toDOM() {
    const el = document.createElement("span");
    el.className = "askw-ed-hr";
    return el;
  }
}

// A task's box, standing in for its `- [ ]`: a click ticks it in the text (and so in the file, on the next save).
class TaskBox extends WidgetType {
  constructor(readonly done: boolean, readonly at: number) { super(); }
  eq(other: TaskBox) { return other.done === this.done && other.at === this.at; }
  toDOM(view: EditorView) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "askw-ed-task";
    box.checked = this.done;
    box.setAttribute("aria-label", "Task");
    box.addEventListener("mousedown", (e) => e.preventDefault());
    box.addEventListener("click", (e) => {
      e.preventDefault();
      view.dispatch({ changes: { from: this.at, to: this.at + 1, insert: this.done ? " " : "x" }, userEvent: "input" });
    });
    return box;
  }
  ignoreEvent() { return true; }
}

// An image drawn in place of its `![](…)`. A click puts the cursor on it, which shows the syntax to edit.
class Picture extends WidgetType {
  constructor(readonly url: string, readonly alt: string, readonly at: number, readonly width: number) { super(); }
  eq(other: Picture) {
    return other.url === this.url && other.alt === this.alt && other.at === this.at && other.width === this.width;
  }
  toDOM(view: EditorView) {
    const img = document.createElement("img");
    img.className = "askw-ed-img";
    img.src = this.url;
    img.alt = this.alt;
    if (this.width) img.width = this.width;
    img.dataset.askwFrom = String(this.at);
    img.addEventListener("load", () => view.requestMeasure());
    return img;
  }
  ignoreEvent() { return false; }
}

interface Cell { runs: Run[]; at: number }
// A table drawn as the page draws it (its <table> sits in the reading column, so it wears the same styles). A click in
// a cell puts the cursor at that cell's text, and the table turns back into its source to edit.
class Grid extends WidgetType {
  constructor(readonly head: Cell[], readonly body: Cell[][], readonly align: string[], readonly key: string) { super(); }
  eq(other: Grid) { return other.key === this.key; }
  get estimatedHeight() { return (this.body.length + 1) * 42; }
  toDOM() {
    const wrap = document.createElement("div");
    wrap.className = "askw-ed-grid";
    const table = document.createElement("table");
    const row = (cells: Cell[], tag: "th" | "td") => {
      const tr = document.createElement("tr");
      cells.forEach((cell, i) => {
        const el = document.createElement(tag);
        if (this.align[i]) el.style.textAlign = this.align[i];
        el.dataset.askwFrom = String(cell.at);
        runsDOM(cell.runs, el);
        tr.append(el);
      });
      return tr;
    };
    const thead = document.createElement("thead");
    thead.append(row(this.head, "th"));
    table.append(thead);
    if (this.body.length) {
      const tbody = document.createElement("tbody");
      for (const cells of this.body) tbody.append(row(cells, "td"));
      table.append(tbody);
    }
    wrap.append(table);
    return wrap;
  }
  ignoreEvent() { return false; }
}

function gridOf(state: EditorState, table: SyntaxNode): Grid {
  const doc = state.doc;
  const cells = (row: SyntaxNode | null): Cell[] => row
    ? row.getChildren("TableCell").map((c) => ({ runs: inlineRuns(doc, c), at: c.from }))
    : [];
  const head = cells(table.getChild("TableHeader"));
  const body = table.getChildren("TableRow").map(cells);
  const spec = table.getChildren("TableDelimiter").find((d) => /-/.test(doc.sliceString(d.from, d.to)));
  const align = spec
    ? doc.sliceString(spec.from, spec.to).replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => {
        const t = c.trim();
        return t.startsWith(":") && t.endsWith(":") ? "center" : t.endsWith(":") ? "right" : t.startsWith(":") ? "left" : "";
      })
    : [];
  // The header row sets the width, as on the page (GFM): a short row is filled out, a long one's extra cells dropped.
  const width = head.length;
  const pad = (r: Cell[], at: number) =>
    r.slice(0, width).concat(Array.from({ length: Math.max(0, width - r.length) }, () => ({ runs: [], at })));
  const key = JSON.stringify([doc.sliceString(table.from, table.to), table.from]);
  return new Grid(pad(head, table.from), body.map((r, i) => pad(r, table.getChildren("TableRow")[i].to)), align, key);
}

// Tables replace whole lines, which only a state field may do.
const touchesSel = (state: EditorState, from: number, to: number) =>
  state.selection.ranges.some((r) => r.from <= to && r.to >= from);

function grids(state: EditorState): DecorationSet {
  const out: Range<Decoration>[] = [];
  syntaxTree(state).iterate({
    enter(ref) {
      if (ref.name !== "Table") return;
      const node = ref.node, from = state.doc.lineAt(node.from).from, to = state.doc.lineAt(Math.max(node.from, node.to - 1)).to;
      if (!touchesSel(state, from, to)) out.push(Decoration.replace({ widget: gridOf(state, node), block: true }).range(from, to));
      return false;
    },
  });
  return Decoration.set(out, true);
}

export const tables = StateField.define<DecorationSet>({
  create: grids,
  update(value, tr) {
    if (tr.docChanged || tr.selection || syntaxTree(tr.startState) !== syntaxTree(tr.state)) return grids(tr.state);
    return value;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// MARK: - Images: where they come from, answered by the editor (main.ts) from the server

export interface ImageSource {
  /** The URL to draw an image with; null when it can't be drawn; undefined while it is being asked for. */
  url(kind: "md" | "wiki", target: string): string | null | undefined;
}
export const imageSource = Facet.define<ImageSource, ImageSource | null>({ combine: (v) => v[0] ?? null });
export const imagesChanged = StateEffect.define<null>();

const IMAGE_FILE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i;

// MARK: - The rest of Live Preview, drawn over what is on screen

const hide = Decoration.replace({});
const dim = Decoration.mark({ class: "askw-ed-mark" });
const bullet = Decoration.replace({ widget: new Bullet() });
const rule = Decoration.replace({ widget: new Rule() });
const cls = (name: string) => Decoration.mark({ class: name });
const INLINE: Record<string, [Decoration, string]> = {
  Emphasis: [cls("askw-ed-em"), "EmphasisMark"],
  StrongEmphasis: [cls("askw-ed-strong"), "EmphasisMark"],
  InlineCode: [cls("askw-ed-code"), "CodeMark"],
  Strikethrough: [cls("askw-ed-strike"), "StrikethroughMark"],
  Highlight: [cls("askw-ed-highlight"), "HighlightMark"],
};

function build(view: EditorView): DecorationSet {
  const { state } = view, doc = state.doc, out: Range<Decoration>[] = [];
  const images = state.facet(imageSource);
  const ranges = state.selection.ranges;
  // Markup shows while the selection touches its construct (inclusive, so a cursor just after `**bold**` still sees
  // the stars it would delete); a line's own markers (`#`, `>`) show while the selection is anywhere on that line.
  const touches = (from: number, to: number) => ranges.some((r) => r.from <= to && r.to >= from);
  const onLine = (from: number, to: number) => touches(doc.lineAt(from).from, doc.lineAt(to).to);
  // A block's last line: one that ends in its newline (the frontmatter does) ends on the line before `to`.
  const lastLine = (from: number, to: number) => doc.lineAt(Math.max(from, to - 1)).number;
  const lines = (from: number, to: number, name: string) => {
    for (let n = doc.lineAt(from).number, last = lastLine(from, to); n <= last; n++) {
      out.push(Decoration.line({ class: name }).range(doc.line(n).from));
    }
  };
  const marks = (node: SyntaxNode, name: string, shown: boolean) => {
    for (const m of node.getChildren(name)) out.push((shown ? dim : hide).range(m.from, m.to));
  };
  // An image, drawn when its URL is known and the cursor isn't on it; otherwise its syntax, muted.
  const picture = (node: SyntaxNode, kind: "md" | "wiki", target: string, alt: string, width = 0) => {
    const url = touches(node.from, node.to) || !images ? null : images.url(kind, target);
    if (url) out.push(Decoration.replace({ widget: new Picture(url, alt, node.from, width) }).range(node.from, node.to));
    else out.push(cls("askw-ed-embed").range(node.from, node.to));
  };
  const seen = new Set<number>();  // a block reached from two visible ranges is decorated once

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from, to,
      enter: (ref) => {
        const node = ref.node, name = node.name;
        const heading = /^(ATX|Setext)Heading(\d)$/.exec(name);
        if (heading) {
          if (seen.has(node.from)) return;
          seen.add(node.from);
          const level = heading[2];
          if (heading[1] === "ATX") {
            out.push(Decoration.line({ class: `askw-ed-h askw-ed-h${level}` }).range(doc.lineAt(node.from).from));
            const shown = onLine(node.from, node.to);
            for (const m of node.getChildren("HeaderMark")) {
              if (shown) { out.push(dim.range(m.from, m.to)); continue; }
              // The space after an opening `#` and before a closing one goes with it.
              let a = m.from, b = m.to;
              if (a === doc.lineAt(a).from) { while (b < node.to && doc.sliceString(b, b + 1) === " ") b++; }
              else { while (a > node.from && doc.sliceString(a - 1, a) === " ") a--; }
              out.push(hide.range(a, b));
            }
          } else {
            const underline = node.getChild("HeaderMark");
            const textEnd = underline ? doc.lineAt(underline.from).from - 1 : node.to;
            lines(node.from, Math.max(node.from, textEnd), `askw-ed-h askw-ed-h${level}`);
            if (underline) out.push(dim.range(underline.from, underline.to));
          }
          return;
        }
        if (name in INLINE) {
          const [style, mark] = INLINE[name];
          if (node.to > node.from) out.push(style.range(node.from, node.to));
          marks(node, mark, touches(node.from, node.to));
          return;
        }
        switch (name) {
          case "Link": {
            // A callout's `[!type]` reads as a shortcut reference link; it is the callout's marker, muted like `>`.
            if (/^\[![^\]\n]+\]$/.test(doc.sliceString(node.from, node.to))
              && /^\s*>[\s>]*$/.test(doc.sliceString(doc.lineAt(node.from).from, node.from))) {
              const fold = /[+-]/.test(doc.sliceString(node.to, node.to + 1)) ? 1 : 0;
              out.push(dim.range(node.from, node.to + fold));
              return false;
            }
            const linkMarks = node.getChildren("LinkMark");
            if (linkMarks.length < 2) return;
            const [open, close] = linkMarks, url = node.getChild("URL");
            const textFrom = open.to, textTo = close.from;
            if (textTo <= textFrom) return;  // `[](url)` has nothing to show in its place
            const shown = touches(node.from, node.to);
            const href = url ? doc.sliceString(url.from, url.to) : "";
            out.push(Decoration.mark({
              class: "askw-ed-link",
              attributes: shown || !href ? {} : { "data-askw-href": href },
            }).range(textFrom, textTo));
            if (shown) {
              out.push(dim.range(node.from, textFrom), dim.range(textTo, node.to));
            } else {
              out.push(hide.range(node.from, textFrom), hide.range(textTo, node.to));
            }
            return;  // on into the text, which may be bold or code itself
          }
          case "Autolink": {
            const url = node.getChild("URL");
            if (!url) return;
            const shown = touches(node.from, node.to);
            out.push(Decoration.mark({
              class: "askw-ed-link",
              attributes: shown ? {} : { "data-askw-href": doc.sliceString(url.from, url.to) },
            }).range(url.from, url.to));
            out.push((shown ? dim : hide).range(node.from, url.from), (shown ? dim : hide).range(url.to, node.to));
            return false;
          }
          case "WikiLink": {
            const target = node.getChild("WikiLinkTarget"), alias = node.getChild("WikiLinkAlias");
            if (!target) return;
            const shown = touches(node.from, node.to), label = alias || target;
            out.push(Decoration.mark({
              class: "askw-ed-link askw-ed-wikilink",
              attributes: shown ? {} : { "data-askw-wiki": wikiTarget(doc, target) },
            }).range(label.from, label.to));
            if (shown) {
              out.push(dim.range(node.from, label.from), dim.range(label.to, node.to));
            } else {
              out.push(hide.range(node.from, label.from), hide.range(label.to, node.to));
            }
            return false;
          }
          case "WikiEmbed": {
            const target = node.getChild("WikiLinkTarget"), alias = node.getChild("WikiLinkAlias");
            if (!target) return false;
            const name = doc.sliceString(target.from, target.to), label = alias ? doc.sliceString(alias.from, alias.to) : "";
            if (IMAGE_FILE.test(name.split("#")[0])) {
              // `![[shot.png|300]]` is 300 px wide, as in Obsidian and on the page.
              picture(node, "wiki", name, /^\d+$/.test(label) ? name : label || name, /^\d+$/.test(label) ? +label : 0);
              return false;
            }
            // Another note, embedded: the page links to it (viewer.py draws a ⧉ link), and so does this.
            const shown = touches(node.from, node.to), text = alias || target;
            out.push(Decoration.mark({
              class: "askw-ed-link askw-ed-embedlink",
              attributes: shown ? {} : { "data-askw-wiki": name },
            }).range(text.from, text.to));
            out.push((shown ? dim : hide).range(node.from, text.from), (shown ? dim : hide).range(text.to, node.to));
            return false;
          }
          case "Image": {
            const url = node.getChild("URL"), linkMarks = node.getChildren("LinkMark");
            const alt = linkMarks.length >= 2 ? doc.sliceString(linkMarks[0].to, linkMarks[1].from) : "";
            if (url) picture(node, "md", doc.sliceString(url.from, url.to), alt);
            else out.push(cls("askw-ed-embed").range(node.from, node.to));
            return false;
          }
          case "Blockquote": {
            // `> [!type] Title` is a callout (viewer.py, `_callouts`): its lines wear the type's colour, its first the title's.
            const callout = /^\s*>\s?\[!([^\]\n]+)\]/.exec(doc.lineAt(node.from).text);
            if (!callout) { lines(node.from, node.to, "askw-ed-quote"); return; }
            const type = callout[1].trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
            for (let n = doc.lineAt(node.from).number, last = lastLine(node.from, node.to); n <= last; n++) {
              const edge = (n === doc.lineAt(node.from).number ? " askw-ed-callout-title" : "") + (n === last ? " askw-ed-callout-last" : "");
              out.push(Decoration.line({ class: "askw-ed-callout" + edge, attributes: { "data-callout": type } }).range(doc.line(n).from));
            }
            return;
          }
          case "URL": {
            // A bare address (GFM's autolink): a link, as the reader draws it. One inside a link is that link's.
            const parent = node.parent?.name;
            if (parent === "Link" || parent === "Image" || parent === "Autolink") return;
            out.push(Decoration.mark({
              class: "askw-ed-link",
              attributes: touches(node.from, node.to) ? {} : { "data-askw-href": doc.sliceString(node.from, node.to) },
            }).range(node.from, node.to));
            return;
          }
          case "Hashtag":
            out.push(cls("askw-ed-tag").range(node.from, node.to));
            return;
          case "Comment":
            out.push(cls("askw-ed-comment").range(node.from, node.to));
            return false;
          case "QuoteMark": {
            if (onLine(node.from, node.to)) { out.push(dim.range(node.from, node.to)); return; }
            const after = doc.sliceString(node.to, node.to + 1) === " " ? node.to + 1 : node.to;
            out.push(hide.range(node.from, after));
            return;
          }
          case "ListMark": {
            const item = node.parent, list = item?.parent;
            if (list?.name !== "BulletList" || item?.getChild("Task")) return;  // a task's box stands in for its bullet
            out.push((touches(node.from, node.to) ? dim : bullet).range(node.from, node.to));
            return;
          }
          case "Task": {
            const marker = node.getChild("TaskMarker");
            if (!marker) return;
            const done = /x/i.test(doc.sliceString(marker.from, marker.to));
            const listMark = node.parent?.getChild("ListMark"), bulleted = node.parent?.parent?.name === "BulletList";
            const from = bulleted && listMark ? listMark.from : marker.from;
            if (touches(from, marker.to)) out.push(dim.range(marker.from, marker.to));
            else out.push(Decoration.replace({ widget: new TaskBox(done, marker.from + 1) }).range(from, marker.to));
            if (done && node.to > marker.to) out.push(cls("askw-ed-done").range(marker.to, node.to));
            return;
          }
          case "FencedCode":
          case "CodeBlock": {
            const first = doc.lineAt(node.from).number, last = lastLine(node.from, node.to);
            for (let n = first; n <= last; n++) {
              const edge = (n === first ? " askw-ed-pre-first" : "") + (n === last ? " askw-ed-pre-last" : "");
              out.push(Decoration.line({ class: "askw-ed-pre" + edge }).range(doc.line(n).from));
            }
            if (name === "FencedCode") {
              for (const m of node.getChildren("CodeMark")) out.push(dim.range(m.from, m.to));
              const info = node.getChild("CodeInfo");
              if (info) out.push(dim.range(info.from, info.to));
            }
            return false;
          }
          case "HorizontalRule": {
            out.push((onLine(node.from, node.to) ? dim : rule).range(node.from, node.to));
            return false;
          }
          case "Table":
            // Drawn as a table by `tables` while the cursor is elsewhere; being edited, its source lines line up.
            lines(node.from, node.to, "askw-ed-table");
            return false;
          case "Frontmatter":
            lines(node.from, node.to, "askw-ed-frontmatter");
            return false;
        }
      },
    });
  }
  return Decoration.set(out, true);
}

export const livePreview = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = build(view); }
  update(u: ViewUpdate) {
    if (u.docChanged || u.viewportChanged || u.selectionSet || syntaxTree(u.startState) !== syntaxTree(u.state)
      || u.transactions.some((t) => t.effects.some((e) => e.is(imagesChanged)))) {
      this.decorations = build(u.view);
    }
  }
}, { decorations: (v) => v.decorations });

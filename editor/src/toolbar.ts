// A selection in the editor gets a formatting bar above it, as Obsidian's editing toolbar gives one: copy, checklist,
// heading, quote | bold, italics, highlight, strikethrough, link | code, code block, clear formatting. The ask button
// stays where ask.js puts it, below the selection, and right-click still opens the ask menu: the bar is for writing,
// the button for asking, and they never share a row. (No underline: Markdown has none, and the page shows raw HTML as
// text.)
//
// It shows once a selection is made (a mouse drag released, a keyboard selection held still), and goes on typing, on
// an empty selection, on a right-click, and when the editor loses focus to anything but the bar itself.
import { StateCommand } from "@codemirror/state";
import { EditorView, ViewPlugin, ViewUpdate } from "@codemirror/view";

import { blockquote, checklist, clearFormatting, codeBlock, heading } from "./commands";
import { insertLink, toggleWrap } from "./typing";

const SVG = (paths: string) =>
  `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
// Lucide's icons, as the screenshot's toolbar uses.
const ICON = {
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  checklist: '<rect x="3" y="5" width="6" height="6" rx="1"/><path d="m3 17 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
  heading: '<polyline points="4 7 4 4 20 4 20 7"/><line x1="9" x2="15" y1="20" y2="20"/><line x1="12" x2="12" y1="4" y2="20"/>',
  quote: '<path d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/><path d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/>',
  bold: '<path d="M6 12h9a4 4 0 0 1 0 8H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h7a4 4 0 0 1 0 8"/>',
  italic: '<line x1="19" x2="10" y1="4" y2="4"/><line x1="14" x2="5" y1="20" y2="20"/><line x1="15" x2="9" y1="4" y2="20"/>',
  highlight: '<path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/>',
  strike: '<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" x2="20" y1="12" y2="12"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  codeBlock: '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/><path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>',
  clear: '<path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/>',
};

interface Button { id: string; label: string; icon: string; run: (view: EditorView) => unknown; mark?: string }
const copy = (view: EditorView) => {
  // Through the page's own copy, which the editor answers with the selected text.
  view.focus();
  document.execCommand("copy");
};
const GROUPS: Button[][] = [
  [
    { id: "copy", label: "Copy", icon: ICON.copy, run: copy },
    { id: "checklist", label: "Checklist", icon: ICON.checklist, run: checklist },
    { id: "heading", label: "Heading", icon: ICON.heading, run: () => undefined },
    { id: "quote", label: "Quote", icon: ICON.quote, run: blockquote },
  ],
  [
    { id: "bold", label: "Bold (⌘B)", icon: ICON.bold, run: toggleWrap("**"), mark: "**" },
    { id: "italic", label: "Italics (⌘I)", icon: ICON.italic, run: toggleWrap("*"), mark: "*" },
    { id: "highlight", label: "Highlight", icon: ICON.highlight, run: toggleWrap("=="), mark: "==" },
    { id: "strike", label: "Strikethrough", icon: ICON.strike, run: toggleWrap("~~"), mark: "~~" },
    { id: "link", label: "Link (⌘K)", icon: ICON.link, run: insertLink },
  ],
  [
    { id: "code", label: "Code", icon: ICON.code, run: toggleWrap("`"), mark: "`" },
    { id: "codeblock", label: "Code block", icon: ICON.codeBlock, run: codeBlock },
    { id: "clear", label: "Clear formatting", icon: ICON.clear, run: clearFormatting },
  ],
];
const HEADINGS: [string, number][] = [["Heading 1", 1], ["Heading 2", 2], ["Heading 3", 3], ["Heading 4", 4], ["Body text", 0]];

// A wrapped selection (`**word**` with `word` selected): its button shows pressed, as a second press unwraps it.
function wrappedIn(view: EditorView, mark: string): boolean {
  const { state } = view, r = state.selection.main, n = mark.length;
  if (state.sliceDoc(r.from - n, r.from) !== mark || state.sliceDoc(r.to, r.to + n) !== mark) return false;
  // `*` must not be half of a `**`.
  return mark !== "*" || (state.sliceDoc(r.from - 2, r.from) !== "**" || state.sliceDoc(r.from - 3, r.from) === "***");
}

export const toolbar = ViewPlugin.fromClass(class {
  bar: HTMLElement;
  menu: HTMLElement;
  pressed = false;
  timer = 0;
  shown = false;
  acting = false;  // the bar's own command is running: its edit isn't typing

  constructor(readonly view: EditorView) {
    const bar = this.bar = document.createElement("div");
    bar.className = "askw-ed-toolbar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Formatting");
    bar.hidden = true;
    GROUPS.forEach((group, g) => {
      if (g) bar.append(Object.assign(document.createElement("span"), { className: "askw-ed-toolbar-sep" }));
      for (const b of group) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.id = b.id;
        button.title = b.label;
        button.setAttribute("aria-label", b.label);
        button.innerHTML = SVG(b.icon);
        // Pressed without taking focus or the selection from the editor.
        button.addEventListener("mousedown", (e) => e.preventDefault());
        button.addEventListener("click", () => {
          if (b.id === "heading") { this.toggleMenu(); return; }
          this.acting = true;
          try { b.run(view); } finally { this.acting = false; }
          view.focus();
          if (b.id === "copy") this.hide();
          else this.place();
        });
        bar.append(button);
      }
    });
    const menu = this.menu = document.createElement("div");
    menu.className = "askw-ed-toolbar-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const [label, level] of HEADINGS) {
      const item = document.createElement("button");
      item.type = "button";
      item.setAttribute("role", "menuitem");
      item.textContent = label;
      item.className = level ? `askw-ed-toolbar-h${level}` : "";
      item.addEventListener("mousedown", (e) => e.preventDefault());
      item.addEventListener("click", () => {
        this.acting = true;
        try { (heading(level) as StateCommand)(view); } finally { this.acting = false; }
        menu.hidden = true;
        view.focus();
        this.place();
      });
      menu.append(item);
    }
    bar.append(menu);
    document.body.append(bar);

    view.dom.addEventListener("mousedown", this.down);
    document.addEventListener("mouseup", this.up);
    document.addEventListener("contextmenu", this.hideNow);
    window.addEventListener("scroll", this.follow, true);
    window.addEventListener("resize", this.follow);
  }

  down = () => { this.pressed = true; this.hide(); };
  up = () => {
    if (!this.pressed) return;
    this.pressed = false;
    this.later(0);
  };
  hideNow = () => this.hide();
  follow = () => { if (this.shown) this.place(); };

  later(ms: number) {
    clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this.show(), ms);
  }

  update(u: ViewUpdate) {
    if (u.docChanged && !this.acting && u.transactions.some((t) => t.isUserEvent("input.type") || t.isUserEvent("delete"))) { this.hide(); return; }
    if (u.focusChanged && !u.view.hasFocus) { this.hide(); return; }
    if (!u.selectionSet && !u.docChanged) return;
    if (u.state.selection.main.empty) { this.hide(); return; }
    // A keyboard selection shows the bar once it holds still; a drag shows it on release (up).
    if (this.pressed) return;
    if (this.shown) this.place();
    else this.later(350);
  }

  show() {
    const { view } = this;
    if (!view.hasFocus || view.state.selection.main.empty || this.pressed) return;
    this.shown = true;
    // In the note's own colours, read off its page by ask.js (`askwNoteColours`), as the ask button below it is.
    const tone = window.askwNoteColours?.() ?? null;
    this.bar.style.setProperty("--askw-bar-bg", tone ? tone.bg : "");
    this.bar.style.setProperty("--askw-bar-ink", tone ? tone.ink : "");
    this.bar.hidden = false;
    this.place();
  }
  hide() {
    clearTimeout(this.timer);
    this.shown = false;
    this.bar.hidden = true;
    this.menu.hidden = true;
  }
  toggleMenu() { this.menu.hidden = !this.menu.hidden; }

  // Above the selection's first line, centred on it; below its last when there's no room above. Measured in the
  // editor's own measure phase: CodeMirror allows no layout reads during an update.
  place() {
    const { view, bar } = this;
    if (view.state.selection.main.empty) { this.hide(); return; }
    for (const button of bar.querySelectorAll<HTMLButtonElement>("button[data-id]")) {
      const mark = GROUPS.flat().find((b) => b.id === button.dataset.id)?.mark;
      button.classList.toggle("is-on", !!mark && wrappedIn(view, mark));
    }
    view.requestMeasure({
      key: this,
      read: (v) => {
        const sel = v.state.selection.main;
        return { start: v.coordsAtPos(sel.from, 1), end: v.coordsAtPos(sel.to, -1), width: bar.offsetWidth, height: bar.offsetHeight };
      },
      write: ({ start, end, width, height }) => {
        if (!this.shown) return;
        if (!start || !end) { bar.style.visibility = "hidden"; return; }
        bar.style.visibility = "";
        const gap = 10, sameLine = Math.abs(start.top - end.top) < 4;
        const centre = sameLine ? (start.left + end.right) / 2 : start.left + Math.min(width / 2, 200);
        let top = start.top - height - gap, below = false;
        if (top < 8) { top = end.bottom + gap; below = true; }
        bar.dataset.side = below ? "below" : "above";
        bar.style.left = `${Math.max(8, Math.min(centre - width / 2, window.innerWidth - width - 8))}px`;
        bar.style.top = `${Math.max(8, Math.min(top, window.innerHeight - height - 8))}px`;
      },
    });
  }

  destroy() {
    clearTimeout(this.timer);
    this.view.dom.removeEventListener("mousedown", this.down);
    document.removeEventListener("mouseup", this.up);
    document.removeEventListener("contextmenu", this.hideNow);
    window.removeEventListener("scroll", this.follow, true);
    window.removeEventListener("resize", this.follow);
    this.bar.remove();
  }
});

declare global {
  interface Window { askwNoteColours?: () => { bg: string; ink: string } | null }
}

// Onyx's note editor: ⌘E turns a Markdown page in the reader into its own source, drawn as Live Preview (preview.ts),
// and ⌘E again turns it back. ask.js loads this bundle on the first ⌘E and drives it through `window.OnyxEditor`.
//
// It saves as Obsidian does, by itself, a moment after typing stops, and at once on ⌘S, on leaving the editor, when
// focus leaves the page, and as the page goes away. Each save names the version of the file it was written against;
// the server refuses one whose file changed on disk meanwhile (another editor, an agent, a sync), and the editor asks
// which to keep instead of overwriting either.
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { ensureSyntaxTree, Language, LanguageSupport, syntaxTree } from "@codemirror/language";
import { commonmarkLanguage, markdownKeymap } from "@codemirror/lang-markdown";
import { yamlFrontmatter } from "@codemirror/lang-yaml";
import { EditorSelection, EditorState, Prec, StateCommand, StateEffect, StateField } from "@codemirror/state";
import { Decoration, DecorationSet, EditorView, keymap } from "@codemirror/view";
import { Autolink, parser as commonmark, Strikethrough, Table, TaskList } from "@lezer/markdown";

import { lineChanges } from "./diff";
import { comments, hashtags, highlights, ImageSource, imageSource, imagesChanged, inlineRuns, livePreview, tables, wikilinks } from "./preview";
import { CSS } from "./style";

/**
 * Where the top of the window is in a note, in terms the reading page and the editor share: the note's top-level blocks
 * (a heading, a paragraph, a whole list), which the reader's renderer and this parser split alike, both being
 * CommonMark. `index` of `count` is the block at the top; `fraction` how far down it the window's top edge sits, or,
 * when the block starts below that edge, 0 with `offset` its distance from it.
 */
export interface Landing { index: number; count: number; fraction: number; offset: number }

export interface OpenOptions {
  /** The reading page's column (`<main>`): the editor takes its place there, and the rendered note is hidden. */
  container: HTMLElement;
  server: string;
  token: string;
  src: string;
  cap: string;
  landing: Landing | null;
  toast(message: string): void;
  /** The editor has closed. `changed` says the file differs from what the page last rendered. */
  onExit(result: { changed: boolean; sig: string; landing: Landing | null }): void;
}

export interface Session {
  exit(): Promise<void>;
  /** The file's signature from the page's live-reload poll. */
  poll(sig: string): void;
}

// CommonMark with tables, strikethrough, highlights, tasks, bare URLs, `%%comments%%` and `#tags`, as the reader renders (viewer.py), plus wikilinks. The Language is built here rather than
// through `markdown()` so the HTML and JavaScript grammars that bundles for inline HTML stay out of this file: the
// reader escapes raw HTML, so there is nothing for them to highlight. It shares `commonmarkLanguage`'s data, which is
// what the Markdown keymap checks, so Enter still continues a list and Backspace still lifts one.
const markdownSupport = new LanguageSupport(
  new Language(commonmarkLanguage.data, commonmark.configure([Table, Strikethrough, TaskList, Autolink, highlights, wikilinks, comments, hashtags]), [], "markdown"),
  [Prec.high(keymap.of(markdownKeymap))],
);

// ⌘B and ⌘I, as Obsidian binds them: wrap the selection, or unwrap it when it is already wrapped.
function toggleWrap(mark: string): StateCommand {
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

// The note's top-level Markdown blocks, as the reader renders each one as a child of its `<main>`. A link reference
// definition renders nothing there, and the frontmatter is the Properties box, so neither is counted.
const BLOCK = /^(?:(?:ATX|Setext)Heading\d|Paragraph|BulletList|OrderedList|Blockquote|FencedCode|CodeBlock|HorizontalRule|Table|HTMLBlock|CommentBlock|ProcessingInstructionBlock)$/;
function sourceBlocks(state: EditorState): { from: number; to: number }[] {
  const tree = ensureSyntaxTree(state, state.doc.length, 250) ?? syntaxTree(state), out: { from: number; to: number }[] = [];
  tree.iterate({
    enter(n) {
      if (!BLOCK.test(n.name) || n.node.parent?.name !== "Document") return;
      out.push({ from: n.from, to: n.to });
      return false;
    },
  });
  return out;
}
// Should the two ever split a note differently, the block at the same share of the way through.
function scaleIndex(index: number, count: number, total: number): number {
  if (!total) return -1;
  if (count === total || count < 2) return Math.min(index, total - 1);
  return Math.min(total - 1, Math.round(index * (total - 1) / (count - 1)));
}

// ⌘F's matches while the editor is open (find_ui.py searches the note's text through window.askwEditor).
const setFind = StateEffect.define<{ ranges: { from: number; to: number }[]; current: number }>();
const findMarks = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, tr) {
    value = value.map(tr.changes);
    for (const e of tr.effects) {
      if (!e.is(setFind)) continue;
      const len = tr.state.doc.length;
      value = Decoration.set(e.value.ranges.flatMap((r, i) => r.to > r.from && r.to <= len
        ? [Decoration.mark({ class: i === e.value.current ? "askw-ed-find-current" : "askw-ed-find" }).range(r.from, r.to)]
        : []), true);
    }
    return value;
  },
  provide: (f) => EditorView.decorations.from(f),
});

/** What the shell's outline and find bar read while the editor stands in for the page (vault_ui.py, find_ui.py). */
export interface EditorFace {
  live(): boolean;
  text(): string;
  headings(): { level: number; text: string; el: HeadingAnchor }[];
  mark(ranges: { from: number; to: number }[], current: number): void;
  reveal(range: { from: number; to: number }): void;
  top(): number;
  selection(): { from: number; to: number };
  select(range: { from: number; to: number }): void;
  onChange(listener: (() => void) | null): void;
}
// A heading as the outline uses an element: where it is on screen, and a way there. CodeMirror draws only the lines
// near the window, so the heading's line may have no element at all; its place comes from the editor's own measure.
interface HeadingAnchor {
  getBoundingClientRect(): { top: number; bottom: number; left: number; right: number; width: number; height: number };
  scrollIntoView(): void;
  closest(): null;
}

let styled = false;
function injectStyle() {
  if (styled) return;
  styled = true;
  const style = document.createElement("style");
  style.id = "askw-editor-style";
  style.textContent = CSS;
  document.head.appendChild(style);
}

// Under this many bytes a save goes out with `keepalive`, so one sent as the page goes away (a sidebar click, a closed
// tab) still lands; browsers cap a keepalive body at 64 KiB.
const KEEPALIVE_BYTES = 60_000;
const SAVE_AFTER_MS = 700;

export async function open(options: OpenOptions): Promise<Session> {
  const { container, server, token, toast } = options;
  const query = new URLSearchParams({ src: options.src, cap: options.cap });
  const loaded = await fetch(`${server}/api/source?${query}`, { cache: "no-store" })
    .then((r) => r.json())
    .catch(() => ({ ok: false, error: "Onyx isn’t answering." }));
  if (!loaded.ok) throw new Error(loaded.error || "This note can’t be edited.");
  injectStyle();

  const edit: string = loaded.edit, openedSig: string = loaded.sig;
  let changeListener: (() => void) | null = null;

  // Images come from the server, and only ones the note as saved references (/api/source/images), so one just typed
  // shows once the save after it lands: a miss is asked about again after each save.
  const imageUrls = new Map<string, string | null>(), asking = new Set<string>();
  let askTimer = 0;
  const images: ImageSource = {
    url(kind, target) {
      if (/^(https?:\/\/|data:image\/)/i.test(target)) return target;
      const key = `${kind}:${target}`;
      if (imageUrls.has(key)) return imageUrls.get(key);
      asking.add(key);
      clearTimeout(askTimer);
      askTimer = window.setTimeout(askImages, 30);
      return undefined;
    },
  };
  async function askImages() {
    const keys = [...asking];
    asking.clear();
    if (!keys.length || closed) return;
    const refs = keys.map((k) => ({ kind: k.slice(0, k.indexOf(":")), target: k.slice(k.indexOf(":") + 1) }));
    const d = await fetch(`${server}/api/source/images`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token, edit, refs }),
    }).then((r) => r.json()).catch(() => null);
    if (!d || !d.ok || closed) return;
    for (const k of keys) imageUrls.set(k, d.urls[k] ?? null);
    view.dispatch({ effects: imagesChanged.of(null) });
  }
  function retryMissingImages() {
    let missing = false;
    for (const [k, v] of imageUrls) if (v === null) { imageUrls.delete(k); missing = true; }
    if (missing && !closed) view.dispatch({ effects: imagesChanged.of(null) });
  }
  let sig = openedSig;            // the version on disk the editor's text was last in step with
  let saved = loaded.text as string;
  let timer = 0, inflight: Promise<boolean> | null = null, again = false;
  let conflict: string | null = null, failed = false, closed = false;
  // "Keep mine" chose this text over the disk's, so it has to be written even when it is the text last saved (undone
  // back to it, say): otherwise the disk's version stays and the editor calls itself saved.
  let mustWrite = false;
  // Text that may never have reached the file (the page left mid-save, or with a conflict open) is kept here in the
  // app's own storage, and offered back the next time the note is opened for editing.
  const draftKey = "askw:draft:" + options.src;
  let draftPending = false;

  const host = document.createElement("div");
  host.className = "askw-ed-host askw-ed";
  container.append(host);
  const status = document.createElement("div");
  status.className = "askw-ed-status";
  status.setAttribute("role", "status");
  const bar = document.createElement("div");
  bar.className = "askw-ed-conflict";
  bar.hidden = true;
  host.append(bar);
  document.body.append(status);

  const dirty = () => mustWrite || view.state.doc.toString() !== saved;
  function keepDraft() {
    try { localStorage.setItem(draftKey, JSON.stringify({ text: view.state.doc.toString(), at: Date.now() })); } catch (e) { /* storage full or off */ }
  }
  function dropDraft() {
    if (draftPending) return;  // one offered back and not yet answered stays until it is
    try { localStorage.removeItem(draftKey); } catch (e) { /* nothing kept */ }
  }
  function paint() {
    const text = conflict ? "Changed on disk" : failed ? "Not saved" : inflight ? "Saving…" : dirty() ? "Edited" : "Saved";
    status.textContent = "Editing · " + text;
    status.dataset.state = conflict || failed ? "problem" : "ok";
  }

  function showConflict(disk: string) {
    conflict = disk;
    bar.hidden = false;
    bar.innerHTML = "";
    const text = document.createElement("span");
    text.textContent = "This note changed on disk while you were editing it.";
    const mine = document.createElement("button");
    mine.textContent = "Keep mine";
    mine.onclick = () => { sig = disk; conflict = null; mustWrite = true; bar.hidden = true; save(); };
    const theirs = document.createElement("button");
    theirs.textContent = "Use the one on disk";
    theirs.onclick = () => { conflict = null; bar.hidden = true; reloadFromDisk(true); };
    bar.append(text, mine, theirs);
    paint();
  }

  // One save at a time; a change made while one is out is sent right after it, against the version it returns.
  function save(): Promise<boolean> {
    clearTimeout(timer);
    if (conflict) return Promise.resolve(false);
    if (inflight) { again = true; return inflight; }
    const text = view.state.doc.toString();
    if (text === saved && !failed && !mustWrite) return Promise.resolve(true);
    const body = JSON.stringify({ token, edit, text, base: sig });
    inflight = fetch(`${server}/api/source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      // The browser's cap is on bytes sent; a string's length counts UTF-16 units, a third of CJK text's UTF-8 size.
      keepalive: new TextEncoder().encode(body).length < KEEPALIVE_BYTES,
    })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}));
        if (r.status === 409 && d.sig) { showConflict(d.sig); return false; }
        if (!r.ok || !d.ok) throw new Error(d.error || `Onyx answered ${r.status}.`);
        sig = d.sig;
        saved = text;
        failed = false;
        mustWrite = false;
        if (!dirty()) dropDraft();
        retryMissingImages();
        return true;
      })
      .catch((err: Error) => {
        if (!failed) toast("Couldn’t save this note — " + (err.message || "Onyx isn’t answering."));
        failed = true;
        return false;
      })
      .finally(() => {
        inflight = null;
        paint();
        if (again && !conflict && !closed) { again = false; save(); }
      });
    paint();
    return inflight;
  }

  // Everything typed so far on disk: waits out a save in flight, and sends what was typed while it was out. False when
  // that can't happen (a conflict, or Onyx not answering), so nothing that relies on it goes ahead.
  async function settle(): Promise<boolean> {
    for (let tries = 0; tries < 6; tries++) {
      if (inflight) { await inflight; continue; }
      if (conflict) return false;
      if (!dirty() && !failed) return true;
      if (!(await save())) return false;
    }
    return !dirty();
  }

  // Take in the file's text, changing only the lines that differ, so a cursor away from them stays put.
  async function reloadFromDisk(force: boolean) {
    const fresh = await fetch(`${server}/api/source/current?${new URLSearchParams({ edit })}`, { cache: "no-store" })
      .then((r) => r.json()).catch(() => null);
    if (!fresh || !fresh.ok || closed) return;
    if (!force && dirty()) { showConflict(fresh.sig); return; }
    const next = fresh.text as string, changes = lineChanges(view.state.doc.toString(), next);
    saved = next;
    sig = fresh.sig;
    failed = false;
    mustWrite = false;
    if (changes.length) view.dispatch({ changes, userEvent: "sync" });
    paint();
  }

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: saved,
      extensions: [
        history(),
        EditorView.lineWrapping,
        yamlFrontmatter({ content: markdownSupport }),
        livePreview,
        tables,
        findMarks,
        imageSource.of(images),
        keymap.of([
          { key: "Mod-s", run: () => { save(); return true; }, preventDefault: true },
          { key: "Mod-b", run: toggleWrap("**") },
          { key: "Mod-i", run: toggleWrap("*") },
          indentWithTab,
          ...defaultKeymap,
          ...historyKeymap,
        ]),
        EditorView.contentAttributes.of({ spellcheck: "true", autocorrect: "on", autocapitalize: "sentences" }),
        EditorView.updateListener.of((u) => {
          if (!u.docChanged) return;
          changeListener?.();
          if (u.transactions.some((t) => t.isUserEvent("sync"))) return;
          clearTimeout(timer);
          timer = window.setTimeout(save, SAVE_AFTER_MS);
          paint();
        }),
        EditorView.domEventHandlers({
          mousedown: (event, v) => followLink(event, v) || enterDrawn(event, v),
          blur: () => { save(); return false; },
        }),
      ],
    }),
  });
  document.body.classList.add("askw-editing");

  // A link drawn as a link (its syntax hidden) follows on click, as in Obsidian; one being edited takes the cursor.
  function followLink(event: MouseEvent, v: EditorView): boolean {
    if (event.button !== 0) return false;
    const el = (event.target as Element).closest?.("[data-askw-href],[data-askw-wiki]");
    if (!el || !v.contentDOM.contains(el)) return false;
    event.preventDefault();
    const wiki = el.getAttribute("data-askw-wiki"), href = el.getAttribute("data-askw-href") || "";
    const newTab = event.metaKey || event.ctrlKey;
    if (!wiki && (/^[a-z][a-z0-9+.-]*:/i.test(href) && !/^file:/i.test(href))) {
      go(href, newTab, true);
      return true;
    }
    if (!wiki && href.startsWith("#")) return true;
    const q = new URLSearchParams({ edit, target: wiki || href, kind: wiki ? "wiki" : "md" });
    fetch(`${server}/api/source/link?${q}`, { cache: "no-store" }).then((r) => r.json()).then((d) => {
      if (d.ok && d.href) go(d.href, newTab, false);
      else toast(wiki ? `No note named “${wiki.split("#")[0]}”` : "That link doesn’t lead to a local page.");
    }).catch(() => toast("Onyx isn’t answering."));
    return true;
  }
  // A click on something drawn in place of its source (a table's cell, an image) puts the cursor there, which shows the
  // source to edit, as in Obsidian.
  function enterDrawn(event: MouseEvent, v: EditorView): boolean {
    if (event.button !== 0) return false;
    const el = (event.target as Element).closest?.("[data-askw-from]");
    if (!el || !v.contentDOM.contains(el)) return false;
    event.preventDefault();
    v.dispatch({ selection: { anchor: Math.min(+(el.getAttribute("data-askw-from") || 0), v.state.doc.length) } });
    v.focus();
    return true;
  }
  // Through a real link in the page, so the shell treats it as it treats the reader's own: ⌘-click for a new tab, an
  // outside address to the top window.
  async function go(href: string, newTab: boolean, outside: boolean) {
    if (!newTab && !(await settle())) return;
    const a = document.createElement("a");
    a.href = href;
    a.hidden = true;
    if (outside) { a.target = "_top"; a.rel = "noreferrer noopener"; }
    document.body.append(a);
    a.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, metaKey: newTab, button: 0 }));
    a.remove();
  }

  // What the reader was showing at the top of the window, shown there again.
  if (options.landing) {
    const blocks = sourceBlocks(view.state), at = options.landing;
    const block = blocks[scaleIndex(at.index, at.count, blocks.length)];
    if (block) {
      const doc = view.state.doc, first = doc.lineAt(block.from).number, last = doc.lineAt(Math.max(block.from, block.to - 1)).number;
      const line = doc.line(Math.min(last, first + Math.floor(at.fraction * (last - first + 1))));
      view.dispatch({
        selection: { anchor: line.from },
        effects: EditorView.scrollIntoView(line.from, { y: "start", yMargin: at.fraction ? 0 : Math.max(0, at.offset) }),
      });
    }
  }
  view.focus();
  paint();

  // A draft left from last time: dropped if the file already has it, else offered back beside the file's text.
  try {
    const kept = JSON.parse(localStorage.getItem(draftKey) || "null");
    if (kept && typeof kept.text === "string" && kept.text !== saved) offerDraft(kept.text, kept.at);
    else localStorage.removeItem(draftKey);
  } catch (e) { /* nothing kept */ }
  function offerDraft(text: string, at: number) {
    draftPending = true;
    bar.hidden = false;
    bar.innerHTML = "";
    const say = document.createElement("span");
    const when = new Date(at || Date.now()).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    say.textContent = `Edits from ${when} never reached the file.`;
    const restore = document.createElement("button");
    restore.textContent = "Restore them";
    restore.onclick = () => {
      draftPending = false;
      bar.hidden = true;
      view.dispatch({ changes: lineChanges(view.state.doc.toString(), text), userEvent: "input" });
    };
    const discard = document.createElement("button");
    discard.textContent = "Discard them";
    discard.onclick = () => { draftPending = false; bar.hidden = true; dropDraft(); };
    bar.append(say, restore, discard);
  }

  const face: EditorFace = {
    live: () => !closed,
    text: () => view.state.doc.toString(),
    headings() {
      const state = view.state, tree = ensureSyntaxTree(state, state.doc.length, 200) ?? syntaxTree(state);
      const out: { level: number; text: string; el: HeadingAnchor }[] = [];
      tree.iterate({
        enter(n) {
          const m = /^(?:ATX|Setext)Heading(\d)$/.exec(n.name);
          if (!m) return;
          const text = inlineRuns(state.doc, n.node).map((r) => r.t).join("").replace(/\s+/g, " ").trim();
          if (text) out.push({ level: +m[1], text, el: anchorAt(n.from) });
          return false;
        },
      });
      return out;
    },
    mark(ranges, current) { view.dispatch({ effects: setFind.of({ ranges, current }) }); },
    reveal(range) {
      const block = view.lineBlockAt(range.from), top = block.top + view.documentTop;
      if (top < 56 || top + block.height > window.innerHeight - 24) {
        view.dispatch({ effects: EditorView.scrollIntoView(range.from, { y: "center" }) });
      }
    },
    top: () => view.lineBlockAtHeight(Math.max(0, -view.documentTop)).from,
    selection: () => ({ from: view.state.selection.main.from, to: view.state.selection.main.to }),
    select(range) { view.dispatch({ selection: { anchor: range.from, head: range.to } }); view.focus(); },
    onChange(listener) { changeListener = listener; },
  };
  function anchorAt(pos: number): HeadingAnchor {
    return {
      getBoundingClientRect() {
        const block = view.lineBlockAt(Math.min(pos, view.state.doc.length)), top = block.top + view.documentTop;
        return { top, bottom: top + block.height, left: 0, right: 1, width: 1, height: block.height };
      },
      scrollIntoView() { view.dispatch({ effects: EditorView.scrollIntoView(pos, { y: "start", yMargin: 16 }) }); },
      closest: () => null,
    };
  }
  window.askwEditor = face;

  // The page going away (a sidebar click, a closed tab): whatever is unsaved is kept as a draft first, since this page
  // can't see a save through or ask about a conflict any more, then sent with keepalive. A save that lands leaves a
  // draft equal to the file, which the next open drops unasked.
  const flushOnHide = () => {
    if (!dirty() && !inflight) return;
    if (dirty() || again) keepDraft();
    if (!conflict) save();
  };
  window.addEventListener("pagehide", flushOnHide);
  const onVisibility = () => { if (document.hidden) flushOnHide(); };
  document.addEventListener("visibilitychange", onVisibility);

  function topLanding(): Landing | null {
    const blocks = sourceBlocks(view.state), edge = -view.documentTop;
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      const top = view.lineBlockAt(b.from).top, bottom = view.lineBlockAt(Math.max(b.from, b.to - 1)).bottom;
      if (bottom <= edge) continue;
      return top >= edge
        ? { index: i, count: blocks.length, fraction: 0, offset: top - edge }
        : { index: i, count: blocks.length, fraction: (edge - top) / Math.max(1, bottom - top), offset: 0 };
    }
    return null;
  }

  return {
    async exit() {
      if (closed) return;
      if (!(await settle())) {
        if (!conflict) toast("This note isn’t saved yet, so it stays open for editing.");
        return;
      }
      closed = true;
      const landing = topLanding();
      window.removeEventListener("pagehide", flushOnHide);
      document.removeEventListener("visibilitychange", onVisibility);
      if (window.askwEditor === face) delete window.askwEditor;
      view.destroy();
      host.remove();
      status.remove();
      document.body.classList.remove("askw-editing");
      options.onExit({ changed: sig !== openedSig, sig, landing });
    },
    poll(disk: string) {
      if (closed || inflight || conflict || disk === sig) return;
      reloadFromDisk(false);
    },
  };
}

declare global {
  interface Window { OnyxEditor?: { open: typeof open }; askwEditor?: EditorFace }
}
window.OnyxEditor = { open };

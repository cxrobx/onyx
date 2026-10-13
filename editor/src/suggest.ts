// Obsidian's suggesters, from the edited note's own vault (the /api/source/* routes, which send names, never text):
//   `[[`       the notes it can link to, narrowed as the name is typed;
//   `[[Note#`  that note's headings, and `[[#` this one's;
//   `#`        the tags the vault uses, the most used first.
// Enter or Tab puts the pick in (a link steps past its `]]`, a tag adds a space); Escape, or typing on, leaves what was
// typed.
import { acceptCompletion, autocompletion, Completion, CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { ensureSyntaxTree, syntaxTree } from "@codemirror/language";
import { EditorState, Extension, Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";

import { slashSource } from "./commands";
import { narrow } from "./match";

export interface NoteName { name: string; folder: string; link: string }
export interface Heading { level: number; text: string }
export interface TagCount { tag: string; count: number }

export interface Lookups {
  notes(query: string): Promise<NoteName[]>;
  headings(note: string): Promise<Heading[]>;
  tags(): Promise<TagCount[]>;
}

// A link's target: what Obsidian leaves out of a heading it links to.
const linkText = (text: string) => text.replace(/[[\]|#^]/g, " ").replace(/\s+/g, " ").trim();

// Put a link's target in from `start` (just after its `[[`), and step past the `]]` auto-pairing usually typed already.
function intoLink(target: string, start?: number) {
  return (view: EditorView, _c: Completion, from: number, to: number) => {
    from = start ?? from;
    const closed = view.state.sliceDoc(to, to + 2) === "]]";
    view.dispatch({
      changes: { from, to, insert: closed ? target : target + "]]" },
      selection: { anchor: from + target.length + 2 },
      userEvent: "input.complete",
      scrollIntoView: true,
    });
  };
}

function ownHeadings(state: EditorState): Heading[] {
  const tree = ensureSyntaxTree(state, state.doc.length, 200) ?? syntaxTree(state), out: Heading[] = [];
  tree.iterate({
    enter(n) {
      const m = /^ATXHeading(\d)$/.exec(n.name);
      if (!m) return;
      const text = state.sliceDoc(n.from, n.to).replace(/^ {0,3}#+\s*/, "").replace(/\s+#+\s*$/, "").trim();
      if (text) out.push({ level: +m[1], text });
      return false;
    },
  });
  return out;
}

// Where a `#` isn't a tag: code, and the frontmatter (its tags are a list there, not `#tag`s).
function inCode(state: EditorState, pos: number): boolean {
  for (let node = syntaxTree(state).resolveInner(pos, -1) as { name: string; parent: unknown } | null; node; node = node.parent as typeof node) {
    if (/Code|Frontmatter/.test(node.name)) return true;
  }
  return false;
}

// A list the editor asks for once, as it opens, and answers from from then on. A source that answers at once leaves
// no moment with the list closed: one that waits on the server for each keystroke closes it while it waits, and an
// Enter typed then is a new line instead of the pick.
interface Loaded<T> { ready: T | null; promise: Promise<T> }
function load<T>(asked: Promise<T>, empty: T): Loaded<T> {
  const loaded: Loaded<T> = { ready: null, promise: asked.catch(() => empty) };
  loaded.promise.then((value) => { loaded.ready = value; });
  return loaded;
}
const whenLoaded = <T, R>(l: Loaded<T>, use: (value: T) => R): R | Promise<R> => (l.ready !== null ? use(l.ready) : l.promise.then(use));

export function suggesters(lookups: Lookups): Extension {
  // Asked for as the editor opens: the first walk of a big vault takes a second, and the first `[[` or `#` shouldn't
  // wait it out. Every note's name (the route sends all of them when nothing is typed) and every tag.
  const noteList = load(lookups.notes(""), [] as NoteName[]);
  const tagList = load(lookups.tags(), [] as TagCount[]);
  // A note's headings, asked for once per name while the editor is open.
  const headingCache = new Map<string, Loaded<Heading[]>>();
  function headingsOf(note: string): Loaded<Heading[]> {
    const key = note.trim().toLowerCase();
    if (!headingCache.has(key)) headingCache.set(key, load(lookups.headings(note), []));
    return headingCache.get(key)!;
  }

  function headingsSource(context: CompletionContext) {
    const typed = context.matchBefore(/\[\[[^[\]|#^\n]*#[^[\]|#^\n]*$/);
    if (!typed) return null;
    const hash = typed.text.indexOf("#"), note = typed.text.slice(2, hash);
    const answer = (all: Heading[]): CompletionResult | null => {
      const list = narrow(all, typed.text.slice(hash + 1), (h) => h.text);
      if (!list.length) return null;
      return {
        from: typed.from + hash + 1,
        filter: false,
        // The note's name, as typed, and the heading go in together.
        options: list.map((h) => ({ label: linkText(h.text), detail: "H" + h.level, apply: intoLink(note + "#" + linkText(h.text), typed.from + 2) })),
      };
    };
    return note.trim() ? whenLoaded(headingsOf(note), answer) : answer(ownHeadings(context.state));
  }

  function notesSource(context: CompletionContext) {
    const typed = context.matchBefore(/\[\[[^[\]|#^\n]*$/);
    if (!typed) return null;
    const query = typed.text.slice(2);
    return whenLoaded(noteList, (all): CompletionResult | null => {
      // By name first; then by path, for a name typed with its folder.
      const byName = narrow(all, query, (n) => n.name), seen = new Set(byName);
      const notes = [...byName, ...narrow(all, query, (n) => n.link).filter((n) => !seen.has(n))].slice(0, 30);
      if (!notes.length) return null;
      return {
        from: typed.from + 2,
        filter: false,
        options: notes.map((note) => ({ label: note.link, displayLabel: note.name, detail: note.folder, apply: intoLink(note.link) })),
      };
    });
  }

  function tagsSource(context: CompletionContext) {
    const typed = context.matchBefore(/(?:^|[\s(])#[\p{L}\p{N}_/-]*$/u);
    if (!typed) return null;
    const hash = typed.from + typed.text.indexOf("#");
    if (inCode(context.state, hash)) return null;
    return whenLoaded(tagList, (all): CompletionResult | null => {
      const tags = narrow(all, typed.text.slice(typed.text.indexOf("#") + 1), (t) => t.tag).slice(0, 50);
      if (!tags.length) return null;
      return {
        from: hash + 1,
        filter: false,  // narrowed above, the most used first among equal matches
        options: tags.map((t) => ({
          label: t.tag,
          detail: String(t.count),
          apply: (view: EditorView, _c: Completion, from: number, to: number) => {
            const spaced = /\s/.test(view.state.sliceDoc(to, to + 1));
            view.dispatch({
              changes: { from, to, insert: spaced ? t.tag : t.tag + " " },
              selection: { anchor: from + t.tag.length + 1 },
              userEvent: "input.complete",
            });
          },
        })),
      };
    });
  }

  return [
    autocompletion({ override: [headingsSource, notesSource, tagsSource, slashSource], icons: false, closeOnBlur: true, maxRenderedOptions: 30, interactionDelay: 0, activateOnTypingDelay: 0 }),
    Prec.highest(keymap.of([{ key: "Tab", run: acceptCompletion }])),
  ];
}

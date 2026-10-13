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

export function suggesters(lookups: Lookups): Extension {
  // Asked for as the editor opens: the first walk of a big vault takes a second, and the first `#` shouldn't wait it out.
  const tagList = lookups.tags().catch(() => [] as TagCount[]);

  // A note's headings, asked for once per name while the editor is open.
  const headingCache = new Map<string, Promise<Heading[]>>();
  function headingsOf(note: string): Promise<Heading[]> {
    const key = note.trim().toLowerCase();
    if (!headingCache.has(key)) headingCache.set(key, lookups.headings(note).catch(() => []));
    return headingCache.get(key)!;
  }

  async function headingsSource(context: CompletionContext): Promise<CompletionResult | null> {
    const typed = context.matchBefore(/\[\[[^[\]|#^\n]*#[^[\]|#^\n]*$/);
    if (!typed) return null;
    const hash = typed.text.indexOf("#"), note = typed.text.slice(2, hash);
    const all = note.trim() ? await headingsOf(note) : ownHeadings(context.state);
    const list = narrow(all, typed.text.slice(hash + 1), (h) => h.text);
    if (context.aborted || !list.length) return null;
    return {
      from: typed.from + hash + 1,
      filter: false,
      // The note's name, as typed, and the heading go in together.
      options: list.map((h) => ({ label: linkText(h.text), detail: "H" + h.level, apply: intoLink(note + "#" + linkText(h.text), typed.from + 2) })),
    };
  }

  async function notesSource(context: CompletionContext): Promise<CompletionResult | null> {
    const typed = context.matchBefore(/\[\[[^[\]|#^\n]*$/);
    if (!typed) return null;
    const notes = await lookups.notes(typed.text.slice(2));
    if (context.aborted || !notes.length) return null;
    return {
      from: typed.from + 2,
      filter: false,  // the server has matched them already
      options: notes.map((note) => ({ label: note.link, displayLabel: note.name, detail: note.folder, apply: intoLink(note.link) })),
    };
  }

  async function tagsSource(context: CompletionContext): Promise<CompletionResult | null> {
    const typed = context.matchBefore(/(?:^|[\s(])#[\p{L}\p{N}_/-]*$/u);
    if (!typed) return null;
    const hash = typed.from + typed.text.indexOf("#");
    if (inCode(context.state, hash)) return null;
    const tags = narrow(await tagList, typed.text.slice(typed.text.indexOf("#") + 1), (t) => t.tag).slice(0, 50);
    if (context.aborted || !tags.length) return null;
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
  }

  return [
    autocompletion({ override: [headingsSource, notesSource, tagsSource, slashSource], icons: false, closeOnBlur: true, maxRenderedOptions: 30, interactionDelay: 0, activateOnTypingDelay: 0 }),
    Prec.highest(keymap.of([{ key: "Tab", run: acceptCompletion }])),
  ];
}

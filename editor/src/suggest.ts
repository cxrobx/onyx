// `[[` suggests the notes it can link to, as Obsidian's link suggester does: the names in the edited note's own vault
// (GET /api/source/notes, which never sends their text), narrowed as the name is typed. Enter or Tab puts the name in
// and steps past the closing `]]`; Escape, or typing on past a `|` or `#`, leaves the brackets as typed.
import { acceptCompletion, autocompletion, CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { Extension, Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";

export interface NoteName { name: string; folder: string; link: string }

export function noteLinks(find: (query: string) => Promise<NoteName[]>): Extension {
  async function source(context: CompletionContext): Promise<CompletionResult | null> {
    const typed = context.matchBefore(/\[\[[^[\]|#^\n]*$/);
    if (!typed) return null;
    const notes = await find(typed.text.slice(2));
    if (context.aborted || !notes.length) return null;
    return {
      from: typed.from + 2,
      filter: false,  // the server has matched them already
      options: notes.map((note) => ({
        label: note.link,
        displayLabel: note.name,
        detail: note.folder,
        apply(view: EditorView, _c: unknown, from: number, to: number) {
          const closed = view.state.sliceDoc(to, to + 2) === "]]";
          view.dispatch({
            changes: { from, to, insert: closed ? note.link : note.link + "]]" },
            selection: { anchor: from + note.link.length + 2 },
            userEvent: "input.complete",
            scrollIntoView: true,
          });
        },
      })),
    };
  }
  return [
    autocompletion({ override: [source], icons: false, closeOnBlur: true, maxRenderedOptions: 30, interactionDelay: 0 }),
    Prec.highest(keymap.of([{ key: "Tab", run: acceptCompletion }])),
  ];
}

// An image pasted or dropped into the editor becomes a file, as in Obsidian: the server writes it where Obsidian would
// (the vault's attachment folder, POST /api/source/attachment) and the editor puts its link where it was pasted or
// dropped. A pasted image is named as Obsidian names one, "Pasted image <date and time>"; a dropped file keeps its
// name. Anything else pasted or dropped is left to the other handlers (paste.ts, CodeMirror's own).
import { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

const IMAGE = /^image\/(png|jpeg|gif|webp)$/;
const EXT: Record<string, string> = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" };

/** Writes one image and answers its link (`![[name]]`), or throws with what went wrong. */
export type Attach = (name: string, base64: string) => Promise<string>;

const pad = (n: number) => String(n).padStart(2, "0");
function pastedName(type: string): string {
  const d = new Date();
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  return `Pasted image ${stamp}.${EXT[type]}`;
}

function base64Of(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("That image couldn't be read."));
    reader.readAsDataURL(file);
  });
}

export function imageAttachments(attach: Attach, toast: (message: string) => void): Extension {
  async function put(view: EditorView, files: File[], at: number, pasted: boolean) {
    const links: string[] = [];
    for (const file of files) {
      try {
        // Several pasted at once share the second, so each after the first takes the server's " 1", " 2" ….
        links.push(await attach(pasted ? pastedName(file.type) : file.name, await base64Of(file)));
      } catch (err) {
        toast("Couldn’t add the image — " + ((err as Error).message || "Onyx isn’t answering."));
      }
    }
    if (!links.length) return;
    const pos = Math.min(at, view.state.doc.length), text = links.join("\n");
    view.dispatch({ changes: { from: pos, insert: text }, selection: { anchor: pos + text.length }, userEvent: "input.paste", scrollIntoView: true });
    view.focus();
  }

  return EditorView.domEventHandlers({
    paste(event, view) {
      const files = [...(event.clipboardData?.files ?? [])].filter((f) => IMAGE.test(f.type));
      if (!files.length) return false;
      event.preventDefault();
      const { from, to } = view.state.selection.main;
      if (to > from) view.dispatch({ changes: { from, to }, selection: { anchor: from } });
      put(view, files, from, true);
      return true;
    },
    dragover(event) {
      if (![...(event.dataTransfer?.items ?? [])].some((i) => i.kind === "file" && IMAGE.test(i.type))) return false;
      event.preventDefault();  // a drop is let through only when something takes it
      return true;
    },
    drop(event, view) {
      const files = [...(event.dataTransfer?.files ?? [])].filter((f) => IMAGE.test(f.type));
      if (!files.length) return false;
      event.preventDefault();
      const at = view.posAtCoords({ x: event.clientX, y: event.clientY }) ?? view.state.selection.main.head;
      put(view, files, at, false);
      return true;
    },
  });
}

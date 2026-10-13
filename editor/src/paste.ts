// Pasting formatted text (a web page, a document, a spreadsheet) puts in its Markdown, as Obsidian's "Convert pasted
// HTML to Markdown" does by default: links, emphasis, headings, lists, tasks, tables, images and code. ⌘⇧V pastes the
// plain text instead. So does anything whose HTML carries no formatting worth keeping (code copied from an editor or a
// terminal comes as styled lines, which would come out double-spaced), and anything pasted into code.
//
// The HTML is parsed into an inert document (DOMParser), so nothing in it runs or loads.
import { syntaxTree } from "@codemirror/language";
import { Extension, Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import TurndownService from "turndown";
import { gfm } from "turndown-plugin-gfm";

const FORMATTED = "a[href],b,strong,i,em,h1,h2,h3,h4,h5,h6,ul,ol,table,img,pre,code,blockquote,s,del,strike,mark,hr,"
  + "span[style*=bold],span[style*='font-weight:7'],span[style*='font-weight: 7'],span[style*=italic]";

let service: TurndownService | null = null;
function converter(): TurndownService {
  if (service) return service;
  const td = new TurndownService({
    headingStyle: "atx", hr: "---", bulletListMarker: "-", codeBlockStyle: "fenced", emDelimiter: "*", strongDelimiter: "**",
  });
  td.use(gfm);
  td.remove(["style", "script", "meta", "title", "head", "noscript", "template"] as (keyof HTMLElementTagNameMap)[]);
  // Rules added later win. One space after a list marker, and a tab under it, as the editor itself indents.
  td.addRule("listItem", {
    filter: "li",
    replacement(content, node) {
      const parent = node.parentNode as HTMLElement;
      let prefix = "- ";
      if (parent.nodeName === "OL") {
        const start = Number(parent.getAttribute("start") || 1);
        prefix = `${start + Array.prototype.indexOf.call(parent.children, node)}. `;
      }
      const paragraph = /\n$/.test(content);
      content = content.replace(/^\n+/, "").replace(/\n+$/, "") + (paragraph ? "\n" : "");
      return prefix + content.replace(/\n/g, "\n\t") + (node.nextSibling ? "\n" : "");
    },
  });
  td.addRule("strikethrough", { filter: ["del", "s", "strike"] as (keyof HTMLElementTagNameMap)[], replacement: (c) => `~~${c}~~` });
  td.addRule("highlight", { filter: ["mark"] as (keyof HTMLElementTagNameMap)[], replacement: (c) => `==${c}==` });
  // Google Docs wraps a whole copy in <b style="font-weight:normal"> and marks emphasis on spans by their style.
  td.addRule("notBold", {
    filter: (node) => node.nodeName === "B" && /font-weight:\s*(normal|[1-4]00)/.test(node.getAttribute("style") || ""),
    replacement: (c) => c,
  });
  td.addRule("styledSpan", {
    filter: (node) => node.nodeName === "SPAN" && /font-weight:\s*(bold|[6-9]00)|font-style:\s*italic/.test(node.getAttribute("style") || ""),
    replacement(content, node) {
      if (!content.trim()) return content;
      const style = (node as HTMLElement).getAttribute("style") || "";
      let mark = /font-weight:\s*(bold|[6-9]00)/.test(style) ? "**" : "";
      if (/font-style:\s*italic/.test(style)) mark += "*";
      const [, lead, body, trail] = /^(\s*)([\s\S]*?)(\s*)$/.exec(content)!;
      return `${lead}${mark}${body}${[...mark].reverse().join("")}${trail}`;
    },
  });
  return (service = td);
}

/** The Markdown for some pasted HTML, or null when its plain text should go in as it is. */
export function htmlToMarkdown(html: string): string | null {
  const doc = new DOMParser().parseFromString(html, "text/html");
  if (!doc.body.querySelector(FORMATTED)) return null;
  // A spreadsheet's table has no header row; Markdown's needs one, so its first row is taken as it.
  for (const table of doc.body.querySelectorAll("table")) {
    const first = table.rows[0];
    if (!first || first.querySelector("th")) continue;
    for (const cell of [...first.cells]) {
      const th = doc.createElement("th");
      th.innerHTML = cell.innerHTML;
      cell.replaceWith(th);
    }
  }
  // WebKit hands pasted HTML over re-serialised, with the spaces beside a link or a bold word as &nbsp;: plain spaces in
  // Markdown, or `**now**` beside one may not read as bold, and the note carries characters no one typed.
  const md = converter().turndown(doc.body).replace(/\u00a0/g, " ").replace(/^\n+|\s+$/g, "");
  return md || null;
}

let plainUntil = 0;

export const htmlPaste: Extension = [
  // ⌘⇧V: the paste that follows goes in as plain text.
  Prec.highest(keymap.of([{ key: "Mod-Shift-v", run: () => { plainUntil = Date.now() + 1000; return false; } }])),
  EditorView.domEventHandlers({
    paste(event, view) {
      const data = event.clipboardData, html = data?.getData("text/html");
      if (!data || !html || Date.now() < plainUntil) { plainUntil = 0; return false; }
      if (data.types.includes("vscode-editor-data")) return false;
      const at = view.state.selection.main.from;
      for (let node: { name: string; parent: unknown } | null = syntaxTree(view.state).resolveInner(at, -1); node; node = node.parent as typeof node) {
        if (/Code|Frontmatter/.test(node.name)) return false;
      }
      const md = htmlToMarkdown(html);
      if (md === null) return false;
      event.preventDefault();
      view.dispatch(view.state.replaceSelection(md), { userEvent: "input.paste", scrollIntoView: true });
      return true;
    },
  }),
];

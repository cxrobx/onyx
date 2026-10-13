// The editor wears the reading page's look: its column, its body font, and its `--reader-*` colours, so ⌘E changes
// what can be typed, not what the note looks like. These are the reader shell's own defaults (viewer.py,
// `_reading_shell`); when the vault's reading styles are in force, markdown_theme.py gives the editor's heading, emphasis,
// code, link and quote classes the vault's values too, from rules that outrank these.
//
// `body .askw-ed` carries one more element than CodeMirror's own scoped rules, so these win over its base theme
// whichever stylesheet came last.
const MONO = "ui-monospace,SFMono-Regular,Menlo,monospace";

// A callout's colour by its type, from the reader's own `--callout-*` variables (viewer.py, `_CALLOUT_COLOURS`).
const CALLOUT_TYPES: Record<string, string[]> = {
  cyan: ["abstract", "summary", "tldr", "tip", "hint", "important"],
  green: ["success", "check", "done"],
  orange: ["question", "help", "faq", "warning", "caution", "attention"],
  red: ["failure", "fail", "missing", "danger", "error", "bug"],
  purple: ["example"],
  gray: ["quote", "cite"],
};
const CALLOUT_COLOURS = Object.entries(CALLOUT_TYPES).map(([colour, types]) =>
  `body .askw-ed .cm-line.askw-ed-callout:is(${types.map((t) => `[data-callout="${t}"]`).join(",")}){--callout-color:var(--callout-${colour})}`,
).join("\n");

export const CSS = `
body.askw-editing main > :not(.askw-ed-host){display:none!important}
body .askw-ed .cm-editor{background:transparent;color:inherit}
body .askw-ed .cm-editor.cm-focused{outline:none}
body .askw-ed .cm-scroller{font-family:inherit;line-height:inherit;overflow:visible}
body .askw-ed .cm-content{padding:0;caret-color:rgb(var(--reader-accent))}
body .askw-ed .cm-line{padding:0}
body .askw-ed .cm-gutters{position:absolute;top:0;left:-30px;height:100%;border:0;background:transparent;color:rgb(var(--reader-faint))}
body .askw-ed .cm-foldGutter .cm-gutterElement{display:flex;justify-content:center;align-items:flex-end;width:22px;cursor:pointer}
body .askw-ed .askw-ed-fold{display:flex;height:1.65em;align-items:center;opacity:0;transition:opacity .12s;transform:rotate(90deg)}
body .askw-ed .cm-editor:hover .askw-ed-fold{opacity:.5}
body .askw-ed .cm-editor .askw-ed-fold:hover{opacity:1}
body .askw-ed .askw-ed-fold.is-folded{opacity:.8;transform:none}
body .askw-ed .askw-ed-folded{display:inline-block;margin-left:6px;padding:0 6px;border-radius:5px;background:rgb(var(--reader-line)/.07);color:rgb(var(--reader-faint));font-size:.85em;line-height:1.4;cursor:pointer}
body .askw-ed .cm-cursor,body .askw-ed .cm-dropCursor{border-left:2px solid var(--askw-note-caret,rgb(var(--reader-accent)))}
body .askw-ed .cm-editor > .cm-scroller > .cm-selectionLayer .cm-selectionBackground{background:var(--askw-note-sel-blur,rgb(var(--reader-accent)/.14))}
body .askw-ed .cm-editor.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground{background:var(--askw-note-sel,rgb(var(--reader-accent)/.26))}
body .askw-ed .cm-line.askw-ed-h{font-weight:700;line-height:1.2;letter-spacing:-.02em}
body .askw-ed .cm-line.askw-ed-h1{font-size:2.35rem;padding-top:.3em}
body .askw-ed .cm-line.askw-ed-h2{font-size:1.5em;padding-top:.5em}
body .askw-ed .cm-line.askw-ed-h3{font-size:1.17em;padding-top:.35em}
body .askw-ed .cm-line.askw-ed-h4{font-size:1em}
body .askw-ed .cm-line.askw-ed-h5{font-size:.83em}
body .askw-ed .cm-line.askw-ed-h6{font-size:.67em}
body .askw-ed .askw-ed-strong{font-weight:700}
body .askw-ed .askw-ed-em{font-style:italic}
body .askw-ed .askw-ed-code{font-family:${MONO};font-size:.88em;background:rgb(var(--reader-code)/.88);border-radius:4px;padding:.12em .1em}
body .askw-ed .askw-ed-mark{color:rgb(var(--reader-faint))}
body .askw-ed .askw-ed-link{color:rgb(var(--reader-accent));text-decoration:underline;text-underline-offset:2px}
body .askw-ed [data-askw-href],body .askw-ed [data-askw-wiki]{cursor:pointer}
body .askw-ed .askw-ed-embed{color:rgb(var(--reader-muted))}
body .askw-ed .cm-line.askw-ed-quote{padding-left:20px;border-left:3px solid rgb(var(--reader-line)/.16);color:rgb(var(--reader-muted))}
body .askw-ed .cm-line.askw-ed-callout{--callout-color:var(--callout-blue,8 109 221);padding:0 12px 0 24px;background:rgb(var(--callout-color)/.1)}
body .askw-ed .cm-line.askw-ed-callout-title{padding-top:12px;border-radius:4px 4px 0 0;color:rgb(var(--callout-color));font-weight:600}
body .askw-ed .cm-line.askw-ed-callout-last{padding-bottom:12px;border-radius:0 0 4px 4px}
body .askw-ed .cm-line.askw-ed-callout-title.askw-ed-callout-last{border-radius:4px}
body .askw-ed .cm-line.askw-ed-callout-title:not(.askw-ed-callout-last){padding-bottom:.35em}
body .askw-ed .askw-ed-callout-icon{display:inline-flex;vertical-align:-.12em;margin-right:6px}
body .askw-ed .askw-ed-callout-fold{display:inline-flex;vertical-align:-.12em;margin-left:6px;opacity:.7}
body .askw-ed .askw-ed-callout-fold.is-open{transform:rotate(90deg)}
body .askw-ed .cm-line.askw-ed-callout-folded{display:none}
${CALLOUT_COLOURS}
body .askw-ed .askw-ed-fnref{font-size:.75em;vertical-align:super;line-height:0;color:rgb(var(--reader-accent))}
body .askw-ed .askw-ed-tag{padding:1px 6px;border-radius:999px;background:rgb(var(--reader-accent)/.12);color:rgb(var(--reader-accent));font-size:.85em}
body .askw-ed .askw-ed-comment{color:rgb(var(--reader-faint))}
body .askw-ed .askw-ed-bullet{display:inline-block;min-width:.6em;text-align:center;color:rgb(var(--reader-muted))}
body .askw-ed .askw-ed-hr{display:inline-block;width:100%;height:0;vertical-align:middle;border-top:1px solid rgb(var(--reader-line)/.14)}
body .askw-ed .cm-line.askw-ed-pre{font:14px/1.55 ${MONO};background:rgb(var(--reader-code)/.88);padding:0 18px}
body .askw-ed .cm-line.askw-ed-pre-first{padding-top:12px;border-radius:10px 10px 0 0}
body .askw-ed .cm-line.askw-ed-pre-last{padding-bottom:12px;border-radius:0 0 10px 10px}
body .askw-ed .cm-line.askw-ed-pre-first.askw-ed-pre-last{border-radius:10px}
body .askw-ed .cm-line.askw-ed-table{font-family:${MONO};font-size:.88em}
body .askw-ed .askw-ed-strike{text-decoration:line-through}
body .askw-ed .askw-ed-highlight{padding:0 .1em;border-radius:3px;background:rgb(255 208 0/.4)}
body .askw-ed .askw-ed-highlight[data-highlight="red"]{background:rgb(233 49 71/.3)} body .askw-ed-swatch[data-highlight="red"]{background:rgb(233 49 71)} body .askw-ed .askw-ed-highlight[data-highlight="orange"]{background:rgb(236 117 0/.3)} body .askw-ed-swatch[data-highlight="orange"]{background:rgb(236 117 0)} body .askw-ed .askw-ed-highlight[data-highlight="yellow"]{background:rgb(224 172 0/.3)} body .askw-ed-swatch[data-highlight="yellow"]{background:rgb(224 172 0)} body .askw-ed .askw-ed-highlight[data-highlight="green"]{background:rgb(8 185 78/.3)} body .askw-ed-swatch[data-highlight="green"]{background:rgb(8 185 78)} body .askw-ed .askw-ed-highlight[data-highlight="blue"]{background:rgb(8 109 221/.3)} body .askw-ed-swatch[data-highlight="blue"]{background:rgb(8 109 221)} body .askw-ed .askw-ed-highlight[data-highlight="purple"]{background:rgb(120 82 238/.3)} body .askw-ed-swatch[data-highlight="purple"]{background:rgb(120 82 238)}
@media(prefers-color-scheme:dark){body .askw-ed .askw-ed-highlight[data-highlight="red"]{background:rgb(251 70 76/.3)} body .askw-ed-swatch[data-highlight="red"]{background:rgb(251 70 76)} body .askw-ed .askw-ed-highlight[data-highlight="orange"]{background:rgb(233 151 63/.3)} body .askw-ed-swatch[data-highlight="orange"]{background:rgb(233 151 63)} body .askw-ed .askw-ed-highlight[data-highlight="yellow"]{background:rgb(224 222 113/.3)} body .askw-ed-swatch[data-highlight="yellow"]{background:rgb(224 222 113)} body .askw-ed .askw-ed-highlight[data-highlight="green"]{background:rgb(68 207 110/.3)} body .askw-ed-swatch[data-highlight="green"]{background:rgb(68 207 110)} body .askw-ed .askw-ed-highlight[data-highlight="blue"]{background:rgb(2 122 255/.3)} body .askw-ed-swatch[data-highlight="blue"]{background:rgb(2 122 255)} body .askw-ed .askw-ed-highlight[data-highlight="purple"]{background:rgb(168 130 255/.3)} body .askw-ed-swatch[data-highlight="purple"]{background:rgb(168 130 255)}}
body .askw-ed-swatch{display:inline-block;flex:none;width:12px;height:12px;margin-right:8px;border-radius:50%;background:rgb(255 208 0/.55);box-shadow:inset 0 0 0 1px rgb(0 0 0/.12)}
body .askw-ed .askw-ed-done{color:rgb(var(--reader-faint));text-decoration:line-through}
body .askw-ed .askw-ed-task{margin:0 .5em 0 0;vertical-align:-.1em;cursor:pointer}
body .askw-ed .askw-ed-embedlink::before{content:"⧉ ";opacity:.6}
body .askw-ed .askw-ed-img{max-width:100%;height:auto;vertical-align:bottom;border-radius:4px}
body .askw-ed .askw-ed-grid{overflow-x:auto;padding:.3em 0}
body .askw-ed .askw-ed-grid table{margin:0;white-space:normal}
body .askw-ed .askw-ed-grid :is(th,td){cursor:text}
body .askw-ed .askw-ed-find{background:rgb(255 212 0/.4)}
body .askw-ed .askw-ed-find-current{background:rgb(255 150 0);color:rgb(20 20 20)}
body .askw-ed .cm-line.askw-ed-frontmatter{font:13px/1.7 ${MONO};color:rgb(var(--reader-muted));background:rgb(var(--reader-code)/.5);padding:0 14px}
body .askw-ed .cm-tooltip.cm-tooltip-autocomplete{padding:4px;border:1px solid rgb(var(--reader-line)/.14);border-radius:8px;background:rgb(var(--reader-pane));box-shadow:0 8px 24px rgb(0 0 0/.14);font:14px/1.4 -apple-system,BlinkMacSystemFont,sans-serif}
body .askw-ed .cm-tooltip.cm-tooltip-autocomplete > ul{max-height:16em;min-width:16em;max-width:min(32em,90vw);font-family:inherit}
body .askw-ed .cm-tooltip.cm-tooltip-autocomplete > ul > li{display:flex;align-items:baseline;gap:10px;padding:4px 8px;border-radius:5px;color:inherit}
body .askw-ed .cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]{background:rgb(var(--reader-accent)/.16);color:inherit}
body .askw-ed .cm-tooltip-autocomplete .cm-completionLabel{flex:0 1 auto;overflow:hidden;text-overflow:ellipsis}
body .askw-ed .cm-tooltip-autocomplete .cm-completionMatchedText{text-decoration:none;font-weight:600}
body .askw-ed .cm-tooltip-autocomplete .cm-completionDetail{margin-left:auto;font-style:normal;font-size:12px;color:rgb(var(--reader-faint));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.askw-ed-toolbar{--bar-bg:var(--askw-bar-bg,var(--reader-pane));--bar-ink:var(--askw-bar-ink,var(--reader-ink));position:fixed;z-index:2147483598;display:flex;align-items:center;gap:2px;padding:5px;border:1px solid rgb(var(--bar-ink)/.14);border-radius:14px;background:rgb(var(--bar-bg));box-shadow:0 10px 30px rgb(0 0 0/.14),0 1px 3px rgb(0 0 0/.08);color:rgb(var(--bar-ink))}
.askw-ed-toolbar[hidden]{display:none}
.askw-ed-toolbar button[data-id]{display:flex;align-items:center;justify-content:center;width:34px;height:34px;padding:0;border:0;border-radius:9px;background:transparent;color:inherit;opacity:.72;cursor:pointer}
.askw-ed-toolbar button[data-id]:hover{background:rgb(var(--bar-ink)/.08);opacity:1}
.askw-ed-toolbar button[data-id].is-on{background:rgb(var(--bar-ink)/.13);opacity:1}
.askw-ed-toolbar-sep{width:1px;height:22px;margin:0 5px;background:rgb(var(--bar-ink)/.16)}
.askw-ed-toolbar-menu{position:absolute;top:calc(100% + 6px);left:76px;display:flex;flex-direction:column;min-width:150px;padding:4px;border:1px solid rgb(var(--bar-ink)/.14);border-radius:10px;background:rgb(var(--bar-bg));box-shadow:0 10px 30px rgb(0 0 0/.14)}
.askw-ed-toolbar[data-side=below] .askw-ed-toolbar-menu{top:auto;bottom:calc(100% + 6px)}
.askw-ed-toolbar-menu[hidden]{display:none}
.askw-ed-toolbar-menu button{display:flex;align-items:center;padding:6px 10px;border:0;border-radius:6px;background:transparent;color:inherit;font:inherit;font-size:14px;text-align:left;cursor:pointer}
.askw-ed-toolbar-menu button:hover{background:rgb(var(--bar-ink)/.08)}
.askw-ed-toolbar-menu .askw-ed-toolbar-h1{font-size:18px;font-weight:700}
.askw-ed-toolbar-menu .askw-ed-toolbar-h2{font-size:16px;font-weight:700}
.askw-ed-toolbar-menu .askw-ed-toolbar-h3{font-size:15px;font-weight:600}
.askw-ed-toolbar-menu .askw-ed-toolbar-h4{font-weight:600}
.askw-ed-conflict{position:sticky;top:10px;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 18px;padding:9px 12px;border:1px solid rgb(var(--reader-line)/.14);border-radius:10px;background:rgb(var(--reader-pane));box-shadow:0 8px 24px rgb(0 0 0/.12);font-size:14px}
.askw-ed-conflict[hidden]{display:none}
.askw-ed-conflict span{flex:1 1 220px}
.askw-ed-conflict button{padding:5px 10px;border:1px solid rgb(var(--reader-line)/.16);border-radius:7px;background:rgb(var(--reader-code));color:inherit;font:inherit;font-size:13px;cursor:pointer}
.askw-ed-conflict button:first-of-type{background:rgb(var(--reader-accent));border-color:transparent;color:#fff}
.askw-ed-status{position:fixed;left:12px;bottom:12px;z-index:2147483599;padding:4px 10px;border:1px solid rgb(var(--reader-line)/.1);border-radius:999px;background:rgb(var(--reader-pane)/.86);color:rgb(var(--reader-muted));font:500 11.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);pointer-events:none}
.askw-ed-status.is-toned{border-color:rgb(var(--askw-note-ink)/.14);background:rgb(var(--askw-note-bg)/.9);color:rgb(var(--askw-note-ink)/.72)}
.askw-ed-status[data-state=problem]{color:#c2410c}
`;

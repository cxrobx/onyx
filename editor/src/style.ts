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
${CALLOUT_COLOURS}
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
.askw-ed-conflict{position:sticky;top:10px;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 18px;padding:9px 12px;border:1px solid rgb(var(--reader-line)/.14);border-radius:10px;background:rgb(var(--reader-pane));box-shadow:0 8px 24px rgb(0 0 0/.12);font-size:14px}
.askw-ed-conflict[hidden]{display:none}
.askw-ed-conflict span{flex:1 1 220px}
.askw-ed-conflict button{padding:5px 10px;border:1px solid rgb(var(--reader-line)/.16);border-radius:7px;background:rgb(var(--reader-code));color:inherit;font:inherit;font-size:13px;cursor:pointer}
.askw-ed-conflict button:first-of-type{background:rgb(var(--reader-accent));border-color:transparent;color:#fff}
.askw-ed-status{position:fixed;left:12px;bottom:12px;z-index:2147483599;padding:4px 10px;border:1px solid rgb(var(--reader-line)/.1);border-radius:999px;background:rgb(var(--reader-pane)/.86);color:rgb(var(--reader-muted));font:500 11.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);pointer-events:none}
.askw-ed-status[data-state=problem]{color:#c2410c}
`;

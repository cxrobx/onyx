import { Component, MarkdownRenderer, type App } from "obsidian";

import { inOtherMode } from "./color-mode";

export interface MarkdownThemeSnapshot {
  mode: "light" | "dark";
  styles: Record<string, Record<string, string>>;
}

const TYPE = ["color", "font-family", "font-size", "font-weight", "font-style", "line-height", "letter-spacing", "text-transform"];
const SPACE = ["margin", "padding"].flatMap((kind) => ["top", "right", "bottom", "left"].map((side) => `${kind}-${side}`));
const BOX = ["background-color", "border-radius", ...SPACE,
  ...["top", "right", "bottom", "left"].flatMap((side) => ["color", "width", "style"].map((kind) => `border-${side}-${kind}`))];
const LINK = ["color", "font-weight", "text-decoration-line", "text-decoration-color", "text-decoration-thickness", "text-underline-offset"];
const ELEMENTS: Record<string, [string, string[]]> = {
  content: [".markdown-preview-view", [...TYPE, "background-color"]],
  p: ["p", SPACE],
  ...Object.fromEntries([1, 2, 3, 4, 5, 6].map((i) => [`h${i}`, [`h${i}`, [...TYPE, ...BOX]] as [string, string[]]])),
  a: ["a.external-link", LINK],
  "internal-link": ["a.internal-link", LINK],
  strong: ["strong", ["color", "font-weight"]],
  em: ["em", ["color", "font-style"]],
  ul: ["ul", [...BOX, "list-style-type"]],
  ol: ["ol", [...BOX, "list-style-type"]],
  li: ["li", SPACE],
  blockquote: ["blockquote", [...TYPE, ...BOX]],
  pre: ["pre", [...TYPE, ...BOX]],
  code: ["p code", [...TYPE, ...BOX]],
  "pre-code": ["pre code", [...TYPE, ...BOX]],
  table: ["table", [...BOX, "border-collapse", "border-spacing"]],
  th: ["th", [...TYPE, ...BOX]],
  td: ["td", [...TYPE, ...BOX]],
  "row-odd": ["tbody tr:nth-child(odd)", ["background-color"]],
  "row-even": ["tbody tr:nth-child(even)", ["background-color"]],
  hr: ["hr", BOX],
};

// Ordinary Markdown only: no embeds, network assets, or user note contents.
const SAMPLE = `Paragraph with **bold**, *italic*, \`code\`, [link](https://example.invalid), and [[Onyx theme sample]].

# Heading one
## Heading two
### Heading three
#### Heading four
##### Heading five
###### Heading six

- First item
- Second item

1. First item
2. Second item

> A blockquote.

\`\`\`
Code sample
\`\`\`

| Heading | Heading |
| --- | --- |
| Cell | Cell |
| Cell | Cell |

---
`;

/** This mode's snapshot, and the other mode's when it was measured this time (see color-mode.ts). */
export interface MarkdownThemeCapture {
  snapshot: MarkdownThemeSnapshot;
  other?: MarkdownThemeSnapshot;
}

/**
 * Export computed styles, after themes, snippets, and font settings resolve. `measureOther` sees this mode's
 * snapshot and says whether to measure the other mode too: that flips the whole window's styles, so only when the
 * theme may have changed.
 */
export async function captureMarkdownTheme(
  app: App, measureOther: (snapshot: MarkdownThemeSnapshot) => boolean = () => false,
): Promise<MarkdownThemeCapture> {
  const host = document.createElement("div");
  host.className = "workspace-leaf-content is-read-mode";
  host.dataset.type = "markdown";
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:fixed;left:-10000px;top:0;width:1000px;visibility:hidden;pointer-events:none";
  const reading = host.createDiv({ cls: "markdown-reading-view" });
  const preview = reading.createDiv({ cls: "markdown-preview-view markdown-rendered is-readable-line-width" });
  const sizer = preview.createDiv({ cls: "markdown-preview-sizer markdown-preview-section" });
  sizer.style.maxWidth = "var(--file-line-width, 700px)";
  (document.querySelector(".workspace-split.mod-root") ?? document.body).appendChild(host);
  const component = new Component();
  component.load();
  try {
    await MarkdownRenderer.render(app, SAMPLE, sizer, "", component);
    // The synthetic internal link should have the normal resolved-link style.
    sizer.querySelector("a.internal-link")?.classList.remove("is-unresolved");
    // Obsidian replaces native bullets with interactive spans. Our reader uses
    // semantic lists, so measure the theme's underlying native list styles.
    sizer.querySelectorAll("ul.has-list-bullet").forEach((list) => list.classList.remove("has-list-bullet"));
    const read = (): MarkdownThemeSnapshot => {
      // A fallback ground from an earlier read would override what this mode's theme gives the preview.
      preview.style.backgroundColor = "";
      const styles: MarkdownThemeSnapshot["styles"] = {};
      for (const [key, [selector, properties]] of Object.entries(ELEMENTS)) {
        const element = host.querySelector<HTMLElement>(selector);
        if (!element) continue;
        const computed = getComputedStyle(element);
        const declarations: Record<string, string> = {};
        for (const prop of properties) {
          const value = computed.getPropertyValue(prop).trim();
          if (value) declarations[prop] = value;
        }
        // Inherited emphasis colors should keep inheriting inside headings/quotes.
        if ((key === "strong" || key === "em") && element.parentElement &&
            computed.color === getComputedStyle(element.parentElement).color) delete declarations.color;
        styles[key] = declarations;
      }
      const computed = getComputedStyle(preview);
      // Some themes leave the preview transparent, inheriting the workspace surface.
      if (!styles.content["background-color"] || styles.content["background-color"] === "rgba(0, 0, 0, 0)") {
        preview.style.backgroundColor = "var(--background-primary)";
        styles.content["background-color"] = getComputedStyle(preview).backgroundColor;
      }
      styles.content["max-width"] = getComputedStyle(sizer).maxWidth;
      // Capture the reading font even when a theme assigns it on the sizer.
      const paragraph = sizer.querySelector("p");
      if (paragraph) {
        const paragraphStyle = getComputedStyle(paragraph);
        for (const prop of TYPE) styles.content[prop] = paragraphStyle.getPropertyValue(prop).trim();
      }
      return { mode: document.body.classList.contains("theme-dark") || computed.colorScheme === "dark" ? "dark" : "light", styles };
    };
    const snapshot = read();
    if (!measureOther(snapshot)) return { snapshot };
    const other = inOtherMode(document.body, read);
    // A theme that pins its colour scheme can read as one mode both ways: then there is no other mode to send.
    return other && other.mode !== snapshot.mode ? { snapshot, other } : { snapshot };
  } finally {
    component.unload();
    host.remove();
  }
}

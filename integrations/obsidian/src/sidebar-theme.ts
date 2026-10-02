import { TFolder, type App } from "obsidian";

import { inOtherMode } from "./color-mode";

/** What Onyx's vault sidebar needs to look like this vault's file explorer (see sidebar_theme.py). */
export interface SidebarThemeSnapshot {
  mode: "light" | "dark";
  styles: Record<string, Record<string, string>>;
  folders: { name: string; color: string; guide?: string; hover?: string }[];
}

/** This mode's snapshot, and the other mode's when it was measured this time. */
export interface SidebarThemeCapture {
  snapshot: SidebarThemeSnapshot;
  other?: SidebarThemeSnapshot;
}

const TYPE = ["color", "font-family", "font-size", "font-weight", "font-style", "letter-spacing", "line-height", "text-transform"];
const ROW = [...TYPE, "padding-top", "padding-bottom", "border-radius"];
const BORDER = ["top", "right", "bottom", "left"].flatMap((side) => ["color", "width", "style"].map((kind) => `border-${side}-${kind}`));
// Each key and property is on the server's allowlist (sidebar_theme.ELEMENTS).
const ELEMENTS: Record<string, [string, string[]]> = {
  pane: [":scope", ["color", "font-family", "font-size", "font-weight", "letter-spacing", "line-height", "background-color"]],
  folder: [".onyx-sample-folder > .nav-folder-title", ROW],
  file: [".onyx-sample-file > .nav-file-title", ROW],
  active: [".onyx-sample-active > .nav-file-title", ["color", "background-color", "font-weight", "border-radius"]],
  chevron: [".onyx-sample-folder > .nav-folder-title .collapse-icon", ["color", "opacity"]],
  guide: [".onyx-sample-folder > .nav-folder-children", ["border-left-color", "border-left-width", "border-left-style"]],
  search: [".onyx-sample-search input", ["color", "background-color", "font-family", "font-size", "border-radius", ...BORDER]],
};

// The server's own rule (markdown_theme._UNSAFE): a value that fails it would sink the whole snapshot.
const UNSAFE = /[;{}<>\\\x00-\x1f]|\/\*|\*\/|(?:url|var|env|attr|expression)\s*\(/i;
export function safeValue(value: string): string | null {
  const trimmed = value.trim();
  return trimmed && trimmed.length <= 512 && !UNSAFE.test(trimmed) ? trimmed : null;
}

const TRANSPARENT = /^(transparent|rgba\([^)]*,\s*0\))$/;
const LIVE = '.workspace-leaf-content[data-type="file-explorer"]';

/** Top-level folders in the explorer's own order, so a positional rainbow (AnuPpuccin's nth-child) lands the same. */
function topFolders(app: App): string[] {
  const live = Array.from(document.querySelectorAll<HTMLElement>(`${LIVE} .nav-files-container > div > .nav-folder > .nav-folder-title`))
    .map((title) => title.dataset.path ?? "").filter(Boolean);
  const vault = app.vault.getRoot().children.filter((child): child is TFolder => child instanceof TFolder)
    .map((folder) => folder.path).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  // The explorer is a virtual list; a folder scrolled out of it is not in the DOM, so fill in from the vault.
  return [...live, ...vault.filter((name) => !live.includes(name))];
}

/** Obsidian's virtual list opens every list with a 1px × 0.1px pusher — hence AnuPpuccin's nth-child(11n+2). */
function list(parent: HTMLElement, cls = ""): HTMLElement {
  const el = parent.createDiv(cls ? { cls } : undefined);
  el.createDiv().style.cssText = "width:1px;height:0.1px;margin-bottom:0";
  return el;
}

function row(parent: HTMLElement, kind: "folder" | "file", path: string, extra = ""): HTMLElement {
  const item = parent.createDiv({ cls: `tree-item nav-${kind} ${extra}`.trim() });
  const self = item.createDiv({ cls: `tree-item-self is-clickable nav-${kind}-title${kind === "folder" ? " mod-collapsible" : ""}` });
  self.dataset.path = path;
  // createSvg adds its cls as class tokens: a space-separated string throws, so it takes a list.
  if (kind === "folder") self.createDiv({ cls: "tree-item-icon collapse-icon" }).createSvg("svg", { cls: ["svg-icon", "right-triangle"] });
  self.createDiv({ cls: `tree-item-inner nav-${kind}-title-content`, text: path.split("/").pop() ?? path });
  if (kind === "folder") row(list(item, "tree-item-children nav-folder-children"), "file", `${path}/Onyx sample.md`);
  return item;
}

/**
 * Measure the file explorer after the vault's theme and snippets resolve. Reads styles only — never note contents.
 *
 * The copy has Obsidian's own shape (see list/row), and sits inside the real explorer pane when one is open, so the
 * pane's background and every ancestor-scoped rule (a border layout's cream pane inside a black frame) resolve as
 * they do there. With no explorer open, it gets the same chain of workspace wrappers in the left sidebar.
 * `measureOther` measures the other colour mode as well (color-mode.ts).
 */
export function captureSidebarTheme(app: App, measureOther = false): SidebarThemeCapture {
  let mount = document.querySelector<HTMLElement>(LIVE);
  let scaffold: HTMLElement | null = null;
  if (!mount) {
    scaffold = (document.querySelector(".workspace-split.mod-left-split") ?? document.body).createDiv({ cls: "workspace-tabs" });
    mount = scaffold.createDiv({ cls: "workspace-tab-container" }).createDiv({ cls: "workspace-leaf" })
      .createDiv({ cls: "workspace-leaf-content" });
    mount.dataset.type = "file-explorer";
  }
  const container = mount.createDiv({ cls: "nav-files-container onyx-sidebar-sample" });
  container.setAttribute("aria-hidden", "true");
  container.style.cssText = "position:fixed;left:-10000px;top:0;width:320px;visibility:hidden;pointer-events:none";
  const top = list(container);
  const names = topFolders(app);
  const folderItems = (names.length ? names : ["Onyx sample"]).map((name, index) =>
    row(top, "folder", name, index === 0 ? "onyx-sample-folder" : ""));
  row(top, "file", "Onyx sample.md", "onyx-sample-file");
  row(top, "file", "Onyx active.md", "onyx-sample-active").querySelector(".nav-file-title")?.addClass("is-active");
  container.createDiv({ cls: "search-input-container onyx-sample-search" }).createEl("input", { type: "search" });
  try {
    const read = (): SidebarThemeSnapshot => {
      const styles: SidebarThemeSnapshot["styles"] = {};
      for (const [key, [selector, properties]] of Object.entries(ELEMENTS)) {
        const element = selector === ":scope" ? container : container.querySelector<HTMLElement>(selector);
        if (!element) continue;
        const computed = getComputedStyle(element);
        const declarations: Record<string, string> = {};
        for (const prop of properties) {
          const value = safeValue(computed.getPropertyValue(prop));
          if (value) declarations[prop] = value;
        }
        styles[key] = declarations;
      }
      // The explorer is usually transparent over its pane: take the first opaque layer behind it.
      if (!styles.pane["background-color"] || TRANSPARENT.test(styles.pane["background-color"])) {
        let layer: HTMLElement | null = container.parentElement;
        let found = "";
        while (layer && !found) {
          const background = getComputedStyle(layer).backgroundColor;
          if (background && !TRANSPARENT.test(background)) found = background;
          layer = layer.parentElement;
        }
        if (found && safeValue(found)) styles.pane["background-color"] = found;
        else delete styles.pane["background-color"];
      }
      // Hover lives in the theme's variables, not on any resting row: resolve them on a probe in the copy.
      const probe = container.createDiv();
      probe.style.cssText = "background-color:var(--nav-item-background-hover);color:var(--nav-item-color-hover)";
      const hover = getComputedStyle(probe);
      styles.hover = {};
      if (!TRANSPARENT.test(hover.backgroundColor) && safeValue(hover.backgroundColor)) styles.hover["background-color"] = hover.backgroundColor;
      if (safeValue(hover.color)) styles.hover.color = hover.color;
      probe.remove();
      if (!styles.file?.color && styles.pane.color) (styles.file ??= {}).color = styles.pane.color;
      const folders = names.map((name, index) => {
        const title = folderItems[index].querySelector<HTMLElement>(":scope > .nav-folder-title")!;
        const titleStyle = getComputedStyle(title);
        const guide = getComputedStyle(folderItems[index].querySelector<HTMLElement>(":scope > .nav-folder-children")!);
        const tint: SidebarThemeSnapshot["folders"][number] = { name, color: safeValue(titleStyle.color) ?? "" };
        if (guide.borderLeftStyle !== "none" && parseFloat(guide.borderLeftWidth) > 0 && safeValue(guide.borderLeftColor)) {
          tint.guide = guide.borderLeftColor;
        }
        // A rainbow theme tints each folder's hover in its own colour (AnuPpuccin sets it on the title). Read it
        // as a background on a probe inside the title: the raw variable keeps the theme's newlines and tabs.
        const tintProbe = title.createDiv();
        tintProbe.style.cssText = "background-color:var(--nav-item-background-hover)";
        const folderHover = getComputedStyle(tintProbe).backgroundColor;
        tintProbe.remove();
        if (!TRANSPARENT.test(folderHover) && safeValue(folderHover) && folderHover !== styles.hover["background-color"]) tint.hover = folderHover;
        return tint;
      }).filter((tint) => tint.color && tint.name.length <= 255 && !/[\x00-\x1f]/.test(tint.name)).slice(0, 256);
      return { mode: document.body.classList.contains("theme-dark") ? "dark" : "light", styles, folders };
    };
    const snapshot = read();
    if (!measureOther) return { snapshot };
    const other = inOtherMode(document.body, read);
    return other && other.mode !== snapshot.mode ? { snapshot, other } : { snapshot };
  } finally {
    container.remove();
    scaffold?.remove();
  }
}

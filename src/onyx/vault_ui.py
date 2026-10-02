"""The app's shell: a sidebar of Library, Notes and Artifacts beside a same-origin reader iframe.

**Notes** is the Obsidian vault (Markdown, wikilinks). **Artifacts** is a folder
of symlinks to HTML pages anywhere on disk: its top-level folders are projects,
pages are labelled by their ``<title>``, and the **+** panel links more in. It,
and the sidebar's reorganising, only ever touch links and folders inside the
vault — see ``vault.writable_folder`` and ``vault.owned_entry``. **Library**, where the app opens, is both at once:
each vault's tree under its own heading, and, while no page is open, the home
page in the reader's place — open a file or URL, what you had open lately, and
your latest asks. Settings and Recent conversations are the two modals at the
sidebar's foot (``panels_ui.py``); ⌘P opens a third over everything, the search
palette (``palette_ui.py``), and ⌘F a bar over the reader that finds words in the
page it shows (``find_ui.py``). On the reader's right is a panel of two halves:
the page's **Outline**, and the pages **Related** to it — its nearest neighbours
in the same index ⌘P searches, read from the page's own direction instead of a
query (``search.PassageIndex.related``).

The page is deliberately thin. Files are plain ``<a target=reader>`` links, so
the browser does the navigating and keeps the history; the one click script
points a link at the frame of the tab showing (``tabs_ui.py``: each open tab
reads in a frame of its own). The rest of the script builds the tree, keeps
the highlight in sync with whatever the reader currently shows, drives the
filter box, the right-click menus
(``static/app-menu.js``: a row's Reveal in Finder and ⌥ Copy Path; blank
sidebar space and Library headings carry what the tree itself does — Reveal Current Note
and Collapse All), and, in Artifacts, the add panel and reorganising: drag a
row onto a folder, and the menu's New Folder, Rename, Pin to Top and Remove
from Artifacts.

Switching views happens in place, never by loading another page: a load blanks
the glass window for a frame and leaves the tree reading "Loading…" until it is
fetched again, so the whole sidebar flickered. The page carries every view's
chrome and keeps both trees, and ``switchVault`` swaps one for another.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig
from .find_ui import find_markup, find_script, find_style
from .launcher_ui import glass_script, theme_settings, theme_style
from .palette_ui import palette_markup, palette_script, palette_style
from .panels_ui import ICONS, panels_markup, panels_script, panels_style
from .tabs_ui import tabs_markup, tabs_script, tabs_style

# The sidebar's pin: filled while the sidebar is pinned, outlined while it floats and comes out from the left edge.
PIN_ICON = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"><path class="pin-head" d="M6.75 2.25v3.5L4.75 8.5h6.5l-2-2.75v-3.5z"/>'
    '<path d="M5.75 2.25h4.5M8 8.5v5.25"/></svg>'
)

# The outline's glyphs (lucide, on the panels' 24 grid): list-tree for the pane and its toggle, chevrons-down-up /
# chevrons-up-down for the collapse-all button, which shows one or the other.
_LUCIDE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"{cls}>{body}</svg>'
)
OUTLINE_ICON = _LUCIDE.format(
    cls="", body='<path d="M21 12h-8"/><path d="M21 6H8"/><path d="M21 18h-8"/><path d="M3 6v4c0 1.1.9 2 2 2h3"/><path d="M3 10v6c0 1.1.9 2 2 2h3"/>'
)
FOLD_ICONS = _LUCIDE.format(cls=' class="down"', body='<path d="m7 20 5-5 5 5"/><path d="m7 4 5 5 5-5"/>') + _LUCIDE.format(
    cls=' class="up"', body='<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>'
)

# What each view calls itself in the shell. All ride along in the page, so a switch swaps them in place.
VAULT_LABELS = {
    "library": {"name": "Library", "unit": "item", "units": "everything", "tree": "Notes and Artifacts", "empty": ""},
    "notes": {"name": "Vault", "unit": "note", "units": "notes", "tree": "Vault notes", "empty": "Pick a note from the tree."},
    "html": {
        "name": "Artifacts",
        "unit": "page",
        "units": "pages",
        "tree": "Artifacts pages",
        "empty": "Pick a page from the sidebar.",
    },
}
# Library's headings, and where a row sits when Library shows it: the switch's words, not the vault's own name.
GROUP_LABELS = {"notes": "Notes", "html": "Artifacts"}

# The tree's glyphs: a folder that opens with its <details>, and the hover card's rows.
_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"{cls}>{body}</svg>'
FOLDER_PATH = '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.6l1.5 1.5h4.9A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/>'
TREE_ICONS = {
    "shut": _SVG.format(cls=' class="shut"', body=FOLDER_PATH),
    "open": _SVG.format(
        cls=' class="open"',
        body='<path d="M2 11.5v-7A1.5 1.5 0 0 1 3.5 3h2.6l1.5 1.5h4.4A1.5 1.5 0 0 1 13.5 6v1"/>'
        '<path d="M2 11.5l1.6-3.7A1.3 1.3 0 0 1 4.8 7h9a.8.8 0 0 1 .75 1.1l-1.5 3.9a1.5 1.5 0 0 1-1.4 1H3.5A1.5 1.5 0 0 1 2 11.5z"/>',
    ),
    "folder": _SVG.format(cls="", body=FOLDER_PATH),
    "doc": _SVG.format(
        cls="", body='<path d="M4 1.75h5.25L12.5 5v8.25c0 .55-.45 1-1 1H4c-.55 0-1-.45-1-1V2.75c0-.55.45-1 1-1z"/><path d="M9 1.75V5.25h3.5M5.5 8.5h5M5.5 11h3.5"/>'
    ),
    "link": _SVG.format(cls="", body='<path d="M6.5 9.5l3-3M7 4.5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1M9 11.5l-1 1A2.5 2.5 0 0 1 4.5 9l1-1"/>'),
    # A row pinned to the top of its folder (Artifacts): the sidebar's own pin, small.
    "pin": _SVG.format(cls="", body='<path d="M6.75 2.25v3.5L4.75 8.5h6.5l-2-2.75v-3.5z"/><path d="M5.75 2.25h4.5M8 8.5v5.25"/>'),
    # Only the Obsidian look shows it (sidebar_theme): a chevron that turns as its folder opens.
    "chev": '<svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5"/></svg>',
}


def vault_page(
    config: AppConfig,
    settings: dict[str, Any] | None,
    *,
    src: str | None = None,
    reader_query: str | None = None,
    kind: str = "notes",
    sidebar: dict[str, Any] | None = None,
    look: dict[str, Any] | None = None,
    extra: list[dict[str, Any]] | None = None,
) -> str:
    """``extra`` is the other notes vaults, ``{key, label, root}`` each (app ``_extra_vaults``): Notes lists each
    under its own heading after the primary's tree, and Library between Notes and Artifacts."""
    kind = kind if kind in VAULT_LABELS else "notes"
    _glass, theme = theme_settings(settings)
    # The vault's Obsidian file-explorer look (sidebar_theme), in force only when it has CSS.
    sidebar = sidebar or {}
    sidebar_css = sidebar.get("css") or ""
    sidebar_state = json.dumps({"revision": sidebar.get("revision", ""), "folders": sidebar.get("folders", []), "css": sidebar_css}).replace("<", "\\u003c")
    body_class = f"kind-{kind}" + (" obsidian-tree" if sidebar_css else "")
    labels = VAULT_LABELS[kind]
    version = html.escape(__version__)
    shared_style = theme_style(settings)
    # The whole app in the vault's colours (vault_look), while Match vault appearance is on and the plugin has measured it.
    look = look or {}
    look_css = look.get("css") or ""
    look_state = json.dumps(look).replace("<", "\\u003c")
    html_class = ' class="vault-look"' if look_css else ""
    glass_js = glass_script(settings, vault=look if look_css else None)
    initial = html.escape(f"/view?{reader_query}", quote=True) if reader_query else "about:blank"
    initial_src = json.dumps(src or "")
    token = json.dumps(config.token)
    vault_name = labels["name"]
    title = html.escape(f"{Path(src).name} — {vault_name}" if src else vault_name)
    library_active, notes_active, html_active = (
        " class=active aria-current=page" if kind == k else "" for k in ("library", "notes", "html")
    )
    vaults_json = json.dumps(VAULT_LABELS)
    groups_json = json.dumps(GROUP_LABELS)
    extra_json = json.dumps([{"key": v["key"], "label": v["label"], "root": v["root"]} for v in extra or []]).replace("<", "\\u003c")
    scope_json = json.dumps((settings or {}).get("search_scope") if (settings or {}).get("search_scope") in ("primary", "all") else "primary")
    icons_json = json.dumps(TREE_ICONS)
    # Library rests on its home page; the vaults on a line saying what to pick.
    home_hidden = "" if kind == "library" and not src else " hidden"
    empty_hidden = " hidden" if kind == "library" else ""
    default_folder = html.escape(str(config.default_folder), quote=True)
    short_folder = html.escape(str(config.default_folder).replace(str(Path.home()), "~", 1))
    history_icon, settings_icon = ICONS["history"], ICONS["settings"]
    panels_css, panels_html, panels_js = panels_style(), panels_markup(settings), panels_script()
    search_css, search_html, search_js = palette_style(), palette_markup(), palette_script()
    find_css, find_html, find_js = find_style(), find_markup(), find_script()
    tabs_css, tabs_html, tabs_js = tabs_style(), tabs_markup(PIN_ICON), tabs_script()
    # The + and its panel ship with every view (CSS shows them only in Artifacts), so a switch needs no reload.
    add_toggle = (
        '<button id=add-toggle class=add-toggle type=button title="Add pages or a folder" '
        'aria-controls=add-panel aria-expanded=false>+</button>'
    )
    add_panel = """<div id=add-panel class=add-panel hidden><label for=add-dest>Add to</label><select id=add-dest></select>
<div class=add-row><button type=button class="secondary pick" id=add-pick-files>HTML files…</button><button type=button class="secondary pick" id=add-pick-folder>Folder…</button></div>
<div class=add-row><input id=add-path placeholder="Paste a path or file:// URL" spellcheck=false autocomplete=off><button type=button class=secondary id=add-path-go>Link</button></div>
<div class=add-row><input id=add-folder-name placeholder="New folder name" spellcheck=false autocomplete=off><button type=button class=secondary id=add-mkdir>Create</button></div>
<p id=add-status class=field-help>Links point at the originals; nothing is moved or copied.</p></div>"""
    empty_hint = labels["empty"]
    units = labels["units"]
    tree_label = labels["tree"]
    return f"""<!doctype html><html data-theme="{theme}"{html_class}><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
{shared_style}
html,body{{height:100%;overflow:hidden}} button,input,select{{font:inherit}}
/* Three columns at most: the sidebar, the reader, and the outline while it is docked (the outline block, below). */
.shell{{--side-w:260px;--outline-w:250px;grid-template-columns:var(--side-w) minmax(0,1fr);height:100vh;min-height:0;transition:grid-template-columns .15s cubic-bezier(.2,.8,.2,1)}}
body.outline-docked .shell{{grid-template-columns:var(--side-w) minmax(0,1fr) var(--outline-w)}} body.side-unpinned.outline-docked .shell{{grid-template-columns:minmax(0,1fr) var(--outline-w)}}
aside{{display:flex;flex-direction:column;height:100vh;padding:20px 12px 14px;overflow:hidden}} body.native aside{{padding-top:48px}}
.brand{{margin:0 8px 12px}} .brand-name{{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.add-toggle{{display:grid;place-items:center;width:24px;height:24px;padding:0;border:1px solid var(--line-soft);border-radius:7px;background:rgb(var(--ink)/.055);color:rgb(var(--secondary));font-size:16px;line-height:1}} .add-toggle:hover,.add-toggle[aria-expanded=true]{{background:rgb(var(--ink)/.1);color:rgb(var(--ink))}}
.vault-switch{{position:relative;display:grid;grid-template-columns:repeat(3,1fr);gap:2px;margin:0 0 10px;padding:2px;border-radius:8px;background:rgb(var(--ink)/.06)}} .vault-switch a{{position:relative;padding:4px 0;border-radius:6px;color:rgb(var(--secondary));font-size:12px;font-weight:600;text-align:center;text-decoration:none;transition:color .15s}} .vault-switch a:hover,.vault-switch a.active{{color:rgb(var(--ink))}}
/* Switching views: the pill is one piece that slides to the active view (a column wide: the padding box less its 2 px
   padding each side and the two 2 px gaps, in thirds), the sidebar's width glides with it (.shell above, the floating
   panel below), and the + comes and goes. */
.vault-switch::before{{content:"";position:absolute;top:2px;bottom:2px;left:2px;width:calc((100% - 8px) / 3);border-radius:6px;background:rgb(var(--bg-elevated)/.92);box-shadow:0 1px 2px rgb(0 0 0/.12);transition:transform .15s cubic-bezier(.2,.8,.2,1)}} body.kind-notes .vault-switch::before{{transform:translateX(calc(100% + 2px))}} body.kind-html .vault-switch::before{{transform:translateX(calc(200% + 4px))}}
body:not(.kind-html) #add-toggle,body:not(.kind-html) #add-panel{{display:none}}
@media(prefers-reduced-motion:reduce){{.shell,.vault-switch::before,.vault-switch a{{transition:none}}}}
.add-panel{{margin:0 0 10px;padding:10px;border:1px solid var(--line-soft);border-radius:10px;background:rgb(var(--bg-surface)/var(--surface-alpha))}} .add-panel label{{display:block;margin:0 0 4px;color:rgb(var(--secondary));font-size:11px;font-weight:650}} .add-panel select{{width:100%;margin:0 0 8px}}
.add-row{{display:flex;gap:6px;margin:0 0 6px}} .add-row button{{flex:1;padding:5px 8px;border-radius:7px;font-size:12px;font-weight:600;white-space:nowrap}} .add-row input{{flex:1;min-width:0;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:rgb(var(--bg-input)/.88);color:rgb(var(--ink));font-size:12px}} .add-row input+button{{flex:none}}
.secondary{{border:1px solid var(--line-soft);background:rgb(var(--ink)/.055);color:rgb(var(--ink))}} .secondary:hover{{border-color:var(--line);background:rgb(var(--ink)/.09)}} .pick{{display:none}} body.native .pick{{display:block}}
.field-help{{margin:4px 1px 0;color:rgb(var(--muted));font-size:10.5px;line-height:1.4}} .field-help.ok{{color:rgb(var(--good))}} .field-help.bad{{color:rgb(var(--bad))}}
#vault-filter{{width:100%;margin:0 0 10px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:rgb(var(--bg-input)/.88);color:rgb(var(--ink));font-size:12.5px;box-shadow:inset 0 1px 0 rgb(255 255 255/.025)}} #vault-filter:focus,.add-row input:focus{{outline:2px solid rgb(var(--accent)/.26);outline-offset:0;border-color:rgb(var(--accent))}}
/* The tree: one quiet line per row, a folder that opens with its <details>, the rest in the hover card. */
#tree{{flex:1;min-height:0;overflow:auto;margin:0 -6px;padding:2px 6px;font-size:13.5px}} #tree ul{{list-style:none;margin:0;padding:0}} #tree ul ul{{padding-left:25px}}
#tree summary,#tree .file{{display:flex;align-items:center;gap:9px;min-height:30px;margin:1px 0;padding:5px 10px;border-radius:9px;color:rgb(var(--ink)/.84);text-decoration:none;white-space:nowrap;user-select:none;transition:background-color .12s,color .12s}}
#tree summary{{cursor:default;list-style:none}} #tree summary::-webkit-details-marker{{display:none}} #tree .lbl{{min-width:0;overflow:hidden;text-overflow:ellipsis}}
#tree .chev{{display:none;flex:none;width:14px;height:14px;transition:transform .12s}} #tree details[open]>summary .chev{{transform:rotate(90deg)}}
#tree .fold{{display:grid;flex:none;width:16px;height:16px;color:rgb(var(--ink)/.6)}} #tree .fold svg{{grid-area:1/1;width:16px;height:16px}} #tree .fold .open,#tree details[open]>summary .fold .shut{{display:none}} #tree details[open]>summary .fold .open{{display:block}}
#tree summary:hover,#tree .file:hover{{background:rgb(var(--ink)/.045);color:rgb(var(--ink))}} #tree a.file.active{{background:rgb(var(--ink)/.12);color:rgb(var(--ink));font-weight:500}}
#tree summary:focus-visible,#tree .file:focus-visible{{outline:2px solid rgb(var(--accent));outline-offset:-2px}}
#tree .sym{{flex:none;margin-left:auto;color:rgb(var(--faint));font-size:11px;opacity:0;transition:opacity .12s}} #tree summary:hover .sym{{opacity:1}}
#tree .ext{{flex:none;margin-left:auto;padding:0 5px;border-radius:4px;background:rgb(var(--ink)/.07);color:rgb(var(--muted));font-size:9px;font-weight:700;text-transform:uppercase}}
#tree .file.missing{{color:rgb(var(--faint));cursor:help}} #tree .file.missing .lbl{{text-decoration:line-through}} #tree .file.missing .ext{{background:rgb(var(--bad)/.1);color:rgb(var(--bad))}}
#tree .results .file{{flex-direction:column;align-items:flex-start;justify-content:center;gap:0}} #tree .results .lbl{{max-width:100%}} #tree .results small{{max-width:100%;overflow:hidden;color:rgb(var(--muted));font-size:11px;text-overflow:ellipsis}} #tree .none{{padding:6px 10px;color:rgb(var(--muted));font-size:12.5px}}
/* Library: each vault's tree under a quiet heading of its own, its folders at the tree's usual depth beneath it. The
   heading is chrome, not a folder, so it keeps this look under the Obsidian one too: that look styles every row through
   `body.obsidian-tree #tree summary`, and these selectors are more specific. */
#tree li.group+li.group{{margin-top:8px}} #tree .group>details>ul{{padding-left:0}}
#tree li.group>details>summary.group-head{{gap:6px;min-height:26px;margin:0;padding:4px 10px;border-radius:7px;background:transparent;color:rgb(var(--muted));font:600 11px/1.3 var(--ui-font);letter-spacing:.06em;text-transform:uppercase}} #tree li.group>details>summary.group-head:hover{{background:transparent;color:rgb(var(--secondary))}}
#tree li.group>details>summary.group-head::before{{content:"";flex:none;width:5px;height:5px;margin:0 3px 0 1px;border:solid currentColor;border-width:0 1.4px 1.4px 0;transform:rotate(-45deg);transition:transform .12s}} #tree li.group>details[open]>summary.group-head::before{{transform:rotate(45deg)}}
#tree li.group>details>summary.group-head .count{{margin-left:auto;font-weight:500;letter-spacing:0;text-transform:none;opacity:0;transition:opacity .12s}} #tree li.group>details>summary.group-head:hover .count{{opacity:1}}
/* The hover card: the whole title, the page's one line, where it lives, what it is. App chrome, so it takes the app's theme. */
#peek{{position:fixed;z-index:50;width:max-content;min-width:220px;max-width:320px;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:rgb(var(--bg-elevated)/.94);color:rgb(var(--ink));box-shadow:0 18px 44px rgb(0 0 0/.26),inset 0 1px 0 rgb(255 255 255/.06);backdrop-filter:blur(24px) saturate(1.3);-webkit-backdrop-filter:blur(24px) saturate(1.3);pointer-events:none;opacity:0;transform:translateX(-4px);transition:opacity .12s ease,transform .12s ease}}
#peek.show{{opacity:1;transform:none}} #peek[hidden]{{display:none}} #peek p{{margin:0}} #peek .peek-title{{font-size:14px;font-weight:600;line-height:1.35}}
#peek .peek-sum{{margin-top:5px;color:rgb(var(--secondary));font-size:12.5px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
#peek .peek-row{{display:flex;align-items:center;gap:8px;margin-top:9px;color:rgb(var(--secondary));font-size:12.5px}} #peek .peek-row+.peek-row{{margin-top:5px}} #peek .peek-row svg{{flex:none;width:15px;height:15px;color:rgb(var(--muted))}} #peek .peek-row span{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
@media(prefers-reduced-motion:reduce){{#peek{{transition:none;transform:none}}}} @media(prefers-reduced-transparency:reduce){{#peek{{background:rgb(var(--bg-elevated));backdrop-filter:none;-webkit-backdrop-filter:none}}}}
/* The foot: how much is here, then the two modals — Recent conversations and Settings — as cxtasks parks its cog. */
.aside-foot{{position:static;display:flex;align-items:center;gap:2px;margin-top:8px;padding:0 2px 0 8px}} #vault-count{{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
.foot-btn{{display:grid;flex:none;place-items:center;width:24px;height:24px;padding:0;border:0;border-radius:6px;background:transparent;color:rgb(var(--faint));transition:background-color 75ms,color 75ms}} .foot-btn:hover{{background:rgb(var(--ink)/.08);color:rgb(var(--ink))}} .foot-btn svg{{width:14px;height:14px}}
main,body.native main{{position:relative;display:grid;padding:0;overflow:hidden}}
/* The reader's place: one frame per tab (tabs_ui.py), stacked in one cell, and only the tab showing (#reader) is seen.
   The rest are hidden and moved below the window, at full size so their pages keep their place. Hiding alone did not do
   it in the app's WebKit (17.5), whose compositor and scrolling ignore visibility: a hidden frame's composited layers (a
   note's backdrop-filter) stayed drawn over the tab brought forward, and the trackpad kept scrolling the tab that had
   been showing (both seen 2026-09-21 in a real window; both Playwright engines and a software snapshot got it right).
   Opacity is what hid the layers; moving the frames is what gave the scrolling back. Down, not sideways, where no
   sidebar or panel lies to be scrolled in their place. Clipped, not hidden: frames below give an overflow:hidden box room
   to scroll, and the app's WebKit scrolled it to reveal whatever took focus in the page (the Ask menu), lifting the page
   with nothing to scroll it back (2026-09-30). A clip is never a scroller. */
#stage{{position:relative;display:grid;grid-template:minmax(0,1fr)/minmax(0,1fr);min-width:0;min-height:0;overflow:clip}}
#stage>iframe{{grid-area:1/1;display:block;width:100%;height:100%;border:0;background:transparent}} #stage>iframe:not(#reader){{visibility:hidden;opacity:0;transform:translateY(calc(100% + 40px))}}
#reader-empty{{position:absolute;inset:0;display:grid;place-items:center;padding:24px;color:rgb(var(--muted));font-size:14px;text-align:center;pointer-events:none}} #reader-empty[hidden]{{display:none}} #reader-empty a{{pointer-events:auto;color:rgb(var(--accent))}}
/* Library's home, in the reader's place while no page is open: open something, what you had open, what you asked. */
#home{{position:absolute;inset:0;z-index:1;overflow:auto;padding:40px 44px 56px}} body.native #home{{padding-top:52px}} #home[hidden]{{display:none}}
.home-inner{{max-width:900px;margin:0 auto}} #home h2{{margin:0;color:rgb(var(--faint));font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}}
.home-h{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:30px 2px 10px}} .home-h .link{{padding:0;border:0;background:none;color:rgb(var(--accent));font-size:12px}} .home-h .link:hover{{text-decoration:underline}}
.open-row{{display:flex;gap:8px}} .open-row input{{flex:1;min-width:0;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:rgb(var(--bg-input)/.88);color:rgb(var(--ink));font-size:13px}} .open-row button,.open-context button{{flex:none;padding:7px 13px;border-radius:8px;font-weight:600}}
.open-row input:focus,.open-context input:focus{{outline:2px solid rgb(var(--accent)/.26);outline-offset:0;border-color:rgb(var(--accent))}}
.open-context{{margin:7px 2px 0;color:rgb(var(--muted));font-size:11.5px}} .open-context summary{{display:inline;cursor:default;list-style:none}} .open-context summary::-webkit-details-marker{{display:none}} .open-context summary:hover{{color:rgb(var(--ink))}}
.open-context .row{{display:flex;gap:6px;margin-top:6px}} .open-context input{{flex:1;min-width:0;padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:rgb(var(--bg-input)/.88);color:rgb(var(--ink));font-size:12px}}
.home-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px}}
.home-card{{display:flex;flex-direction:column;gap:5px;min-width:0;padding:11px 13px;border:1px solid var(--line-soft);border-radius:10px;background:rgb(var(--bg-surface)/var(--surface-alpha));color:inherit;text-decoration:none;transition:border-color 75ms,background-color 75ms}} .home-card:hover{{border-color:var(--line);background:rgb(var(--bg-surface)/.98)}}
.home-card .t{{overflow:hidden;font-weight:600;white-space:nowrap;text-overflow:ellipsis}} .home-card .m{{display:flex;align-items:center;gap:7px;min-width:0;color:rgb(var(--muted));font-size:11px}} .home-card .w{{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}} .home-card .a{{flex:none;font-variant-numeric:tabular-nums}}
.tag{{flex:none;padding:1px 6px;border-radius:4px;background:rgb(var(--ink)/.07);color:rgb(var(--secondary));font-size:9.5px;font-weight:650;letter-spacing:.03em;text-transform:uppercase}} .tag.notes{{background:rgb(var(--good)/.12);color:rgb(var(--good))}} .tag.html{{background:rgb(var(--accent)/.13);color:rgb(var(--accent))}}
/* An ask is a <button>, and the app's WebKit (17) gives buttons align-items:flex-start: stretch its lines to the row, or they
   shrink to their text, the age sits against the title and a long question runs off the page. */
.home-list{{display:flex;flex-direction:column;gap:1px}} .home-ask{{display:flex;flex-direction:column;align-items:stretch;gap:2px;width:100%;padding:8px 11px;border:0;border-radius:8px;background:transparent;color:inherit;text-align:left;transition:background-color 75ms}} .home-ask:hover,.home-ask:focus-visible{{background:rgb(var(--ink)/.05);outline:none}}
.home-ask .t{{display:flex;align-items:baseline;gap:8px;min-width:0}} .home-ask strong{{flex:1;min-width:0;overflow:hidden;font-weight:600;white-space:nowrap;text-overflow:ellipsis}} .home-ask .a{{flex:none;color:rgb(var(--muted));font-size:11px}} .home-ask .q{{overflow:hidden;color:rgb(var(--secondary));white-space:nowrap;text-overflow:ellipsis}}
.home-empty{{padding:20px;border:1px dashed var(--line);border-radius:10px;color:rgb(var(--muted));text-align:center}}
/* Artifacts are named by sentence-length titles, and Library shows them too: a wider sidebar, one line each, and the whole
   title in the hover card. */
body.kind-html .shell,body.kind-library .shell{{--side-w:290px}}
/* A switch takes the new width at once: the columns' ease is for pinning, and on a switch it reflowed the page every frame. */
body.kind-still .shell{{transition:none!important}}
/* Sidebar: pinned, it sits in the grid; unpinned, it floats over the reader and comes out when the pointer rests on the
   left edge, as in Zen's compact mode: #side-edge, 24 px deep so the pointer needn't find a sliver, laid over the reader
   because its iframe would swallow the pointer (a page's first 24 px are margin; they can't be clicked while unpinned). */
.side-toggle{{display:grid;place-items:center;flex:none;width:26px;height:24px;padding:0;border:0;border-radius:7px;background:transparent;color:rgb(var(--secondary))}} .side-toggle:hover{{background:rgb(var(--ink)/.08);color:rgb(var(--ink))}} .side-toggle svg{{width:16px;height:16px}} #side-pin[aria-pressed=true] .pin-head,#outline-pin[aria-pressed=true] .pin-head{{fill:currentColor}}
@media(max-width:800px){{.shell,body.kind-html .shell,body.kind-library .shell{{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}} aside,body.native aside{{position:static;height:auto;max-height:45vh;padding:12px 12px 8px}} body.native aside{{padding-top:38px}} .aside-foot{{display:flex}} #home{{padding:24px 18px 40px}}}}
#side-edge{{position:fixed;top:0;bottom:0;left:0;z-index:39;display:none;width:24px}} body.side-unpinned #side-edge{{display:block}}
body.side-unpinned .shell{{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr)}}
/* Floating, it is a panel lying on the page: inset, rounded, shadowed, and on a ground thick enough to read over a page's
   text even where the blur is not drawn; the window's glass is too thin (the Obsidian look brings an opaque ground of its own). */
body.side-unpinned #vault-side{{position:fixed;top:8px;bottom:8px;left:8px;z-index:40;width:min(var(--side-w),86vw);height:auto;max-height:none;padding-top:12px;border:1px solid var(--line);border-radius:12px;box-shadow:0 18px 50px rgb(0 0 0/.24),0 2px 8px rgb(0 0 0/.08);visibility:hidden;transform:translateX(calc(-100% - 16px))}} body.native.side-unpinned #vault-side{{padding-top:40px}}
body.side-unpinned:not(.obsidian-tree) #vault-side{{background:rgb(var(--bg-sidebar)/.96);backdrop-filter:blur(24px) saturate(1.3);-webkit-backdrop-filter:blur(24px) saturate(1.3)}}
@media(prefers-reduced-transparency:reduce){{body.side-unpinned:not(.obsidian-tree) #vault-side{{background:rgb(var(--bg-sidebar));backdrop-filter:none;-webkit-backdrop-filter:none}}}}
body.side-unpinned.side-out #vault-side{{visibility:visible;transform:none}}
/* Motion: out with an ease-out slide; away with a quicker ease-in one, hidden only once it is off. Reduce Motion fades in place. */
body.side-unpinned #vault-side{{transition:transform .13s cubic-bezier(.4,0,1,1),visibility 0s linear .13s}} body.side-unpinned.side-out #vault-side{{transition:transform .15s cubic-bezier(.2,.8,.2,1),visibility 0s,width .15s cubic-bezier(.2,.8,.2,1)}} body.side-still #vault-side,body.side-still .shell{{transition:none!important}}
@media(prefers-reduced-motion:reduce){{body.side-unpinned #vault-side{{transform:none;opacity:0;transition:opacity .15s linear,visibility 0s linear .15s}} body.side-unpinned.side-out #vault-side{{opacity:1;transition:opacity .15s linear,visibility 0s}}}}
/* The sidebar's width is the reader's: drag its right edge, pinned or floating (the grip lies inside the edge, since the
   aside clips), double-click to put the default back. While dragging, nothing glides and the reader's iframe can't swallow
   the pointer. One width across Library, Notes and Artifacts, remembered (askw:vault:side-w, inline on .shell). */
#side-grip{{position:absolute;top:0;right:0;bottom:0;z-index:3;width:8px;cursor:col-resize;touch-action:none}}
#side-grip::after{{content:"";position:absolute;top:0;right:1px;bottom:0;width:2px;border-radius:2px;background:rgb(var(--accent));opacity:0;transition:opacity .1s}}
body.side-unpinned #side-grip::after{{top:10px;bottom:10px;right:2px}}
#side-grip:hover::after,body.side-resizing #side-grip::after{{opacity:.7}}
body.side-resizing,body.side-resizing *{{cursor:col-resize!important;user-select:none;-webkit-user-select:none}} body.side-resizing .shell,body.side-resizing #vault-side{{transition:none!important}} body.side-resizing #reader{{pointer-events:none}}
@media(max-width:800px){{#side-grip{{display:none}}}}
/* The row a menu is open for wears a ring, as Finder's does. */
#tree .menu-for,#home .menu-for{{box-shadow:inset 0 0 0 2px rgb(var(--accent))}}
/* Reorganising Artifacts: the row being dragged fades, the folder it would land in wears that ring over a wash (the whole
   list does, for the top level), a pinned row carries a quiet pin, and a name being edited is a field in its row. */
#tree .dragging{{opacity:.45}} #tree .drop-into{{background:rgb(var(--accent)/.12);box-shadow:inset 0 0 0 2px rgb(var(--accent))}} #tree.drop-root{{border-radius:10px;box-shadow:inset 0 0 0 2px rgb(var(--accent)/.55)}}
#tree .pinned{{display:grid;flex:none;margin-left:auto;color:rgb(var(--faint))}} #tree .pinned svg{{width:11px;height:11px}} #tree .pinned+.sym{{margin-left:4px}}
#tree .new-folder>svg{{flex:none;width:16px;height:16px;color:rgb(var(--ink)/.6)}} #tree .lbl:has(.name-edit){{flex:1}}
#tree .name-edit{{width:100%;min-width:0;margin:-3px 0;padding:2px 6px;border:1px solid rgb(var(--accent));border-radius:6px;background:rgb(var(--bg-input));color:rgb(var(--ink));font:inherit;outline:2px solid rgb(var(--accent)/.26)}}
/* Outline: the page's headings on the reader's right, as Obsidian's outline pane. Docked (pinned), it is the grid's third
   column; floating (the default), it lies over the reader like the unpinned sidebar and comes out on the reader's right
   edge (watched in script, not an overlay: the page's scrollbar lives there) or from the round toggle in its corner. */
#outline-side{{border-right:0;border-left:1px solid var(--line-soft)}} #outline-side .brand{{gap:6px;margin-bottom:10px}}
/* The pane's two panels share its header: a segmented switch where the title used to be. The fold button belongs to the
   outline alone and goes while Related shows, which is also what leaves the switch its room. */
.pane-tabs{{display:flex;flex:1;gap:2px;min-width:0;padding:2px;border-radius:8px;background:rgb(var(--ink)/.055)}}
.pane-tab{{flex:1;min-width:0;padding:4px 6px;border:0;border-radius:6px;background:transparent;color:rgb(var(--secondary));font:inherit;font-size:11.5px;font-weight:500;letter-spacing:0;white-space:nowrap;transition:background-color .12s,color .12s}}
.pane-tab:hover{{color:rgb(var(--ink))}} .pane-tab[aria-selected=true]{{background:rgb(var(--bg-elevated));color:rgb(var(--ink));font-weight:600;box-shadow:0 1px 2px rgb(0 0 0/.07)}}
.pane-tab:focus-visible{{outline:2px solid rgb(var(--accent));outline-offset:-2px}} .side-toggle[hidden]{{display:none}}
#outline-filter{{width:100%;margin:0 0 8px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:rgb(var(--bg-input)/.88);color:rgb(var(--ink));font-size:12.5px;box-shadow:inset 0 1px 0 rgb(255 255 255/.025)}} #outline-filter:focus{{outline:2px solid rgb(var(--accent)/.26);outline-offset:0;border-color:rgb(var(--accent))}}
#outline-fold .up,#outline-fold.all-shut .down{{display:none}} #outline-fold.all-shut .up{{display:block}}
#outline{{flex:1;min-height:0;overflow:auto;margin:0 -6px;padding:2px 6px;font-size:13px}} #outline ul{{list-style:none;margin:0;padding:0}} #outline ul ul{{margin-left:11px;padding-left:9px;border-left:1px solid var(--line-soft)}}
#outline .row{{display:flex;align-items:flex-start;gap:2px;margin:1px 0;padding:3px 6px 3px 2px;border-radius:7px;color:rgb(var(--ink)/.84);transition:background-color .12s,color .12s}} #outline .row:hover{{background:rgb(var(--ink)/.045);color:rgb(var(--ink))}}
#outline .h{{flex:1;min-width:0;padding:1px 2px;border:0;border-radius:4px;background:transparent;color:inherit;font:inherit;line-height:1.35;text-align:left;overflow-wrap:anywhere;cursor:default}} #outline .h.active{{color:rgb(var(--ink));font-weight:600}} #outline li:has(> .row > .h.active) > .row{{background:rgb(var(--ink)/.09)}}
#outline .tw{{display:grid;flex:none;place-items:center;width:16px;height:19px;padding:0;border:0;border-radius:4px;background:transparent;color:rgb(var(--faint));visibility:hidden;cursor:default}} #outline li.has-kids>.row .tw{{visibility:visible}} #outline .tw:hover{{background:rgb(var(--ink)/.08);color:rgb(var(--ink))}}
#outline .tw svg{{width:12px;height:12px;transform:rotate(90deg);transition:transform .12s}} #outline li.shut>.row .tw svg{{transform:none}} #outline li.shut>ul{{display:none}} #outline.filtering li.shut>ul{{display:block}} #outline li.miss{{display:none}}
#outline .h:focus-visible,#outline .tw:focus-visible{{outline:2px solid rgb(var(--accent));outline-offset:-2px}} #outline .none{{padding:6px 10px;color:rgb(var(--muted));font-size:12.5px}}
/* Related: the pages nearest this one by meaning. The map above the list puts the nearest by the centre and the ones
   alike to each other together (relLayout), each with its score over it; the list below carries the rest. */
#related{{display:none;flex:1;flex-direction:column;min-height:0}} #related:not([hidden]){{display:flex}}
#rel-map{{display:block;flex:none;width:100%;max-width:210px;height:auto;margin:10px auto 12px;aspect-ratio:1;overflow:visible}} #related.no-map #rel-map{{display:none}}
#rel-map circle{{transition:r .12s,fill-opacity .12s}} .rel-here{{fill:rgb(var(--accent)/.5);stroke:rgb(var(--accent));stroke-width:1.4}}
.rel-dot{{fill:rgb(var(--bg-elevated));stroke:rgb(var(--secondary));stroke-width:1.1;cursor:default}}
.rel-dot.on{{r:4.4;fill:rgb(var(--accent)/.28);stroke:rgb(var(--accent))}} .rel-dot.dupe{{stroke-dasharray:2.2 1.6}}
.rel-lab{{fill:rgb(var(--muted));font-size:4.4px;font-variant-numeric:tabular-nums;text-anchor:middle;pointer-events:none;transition:fill .12s}} .rel-lab.on{{fill:rgb(var(--accent))}}
#rel-list{{flex:1;min-height:0;overflow:auto;margin:0 -6px;padding:0 6px 2px;display:flex;flex-direction:column;gap:1px}}
.rel-row{{display:flex;flex-direction:column;gap:1px;width:100%;padding:5px 8px;border:0;border-radius:7px;background:transparent;color:inherit;font:inherit;text-align:left;cursor:default;transition:background-color .1s}}
.rel-row:hover,.rel-row.on{{background:rgb(var(--ink)/.06)}}
.rel-row:focus-visible{{outline:2px solid rgb(var(--accent));outline-offset:-2px}}
.rel-t{{display:flex;align-items:baseline;gap:6px;min-width:0}} .rel-score{{flex:none;color:rgb(var(--faint));font-size:10px;font-variant-numeric:tabular-nums}}
.rel-name{{flex:1;min-width:0;overflow:hidden;color:rgb(var(--ink)/.88);font-size:12.5px;white-space:nowrap;text-overflow:ellipsis}}
.rel-dupe{{flex:none;padding:0 4px;border-radius:4px;background:rgb(var(--accent)/.16);color:rgb(var(--secondary));font-size:9.5px;letter-spacing:.02em}}
.rel-where{{overflow:hidden;padding-left:24px;color:rgb(var(--muted));font-size:10.5px;white-space:nowrap;text-overflow:ellipsis}}
#related .none{{padding:6px 10px;color:rgb(var(--muted));font-size:12.5px;line-height:1.5}}
.outline-toggle{{position:absolute;top:12px;right:12px;z-index:2;display:grid;place-items:center;width:30px;height:30px;padding:0;border:1px solid var(--line);border-radius:999px;background:rgb(var(--bg-elevated)/.82);color:rgb(var(--secondary));box-shadow:0 6px 18px rgb(0 0 0/.1);backdrop-filter:blur(14px) saturate(1.8);-webkit-backdrop-filter:blur(14px) saturate(1.8);transition:background-color .15s,color .15s}}
.outline-toggle:hover,.outline-toggle[aria-expanded=true]{{background:rgb(var(--bg-elevated)/.97);color:rgb(var(--ink))}} .outline-toggle svg{{width:15px;height:15px}} .outline-toggle:focus-visible{{outline:2px solid rgb(var(--accent));outline-offset:2px}}
body.outline-docked .outline-toggle{{display:none}} body.outline-out .outline-toggle{{visibility:hidden}}
body.reader-blank .outline-toggle{{display:none}}
body:not(.outline-docked) #outline-side{{position:fixed;top:8px;right:8px;bottom:8px;z-index:40;width:min(var(--outline-w),86vw);height:auto;max-height:none;padding-top:12px;border:1px solid var(--line);border-radius:12px;box-shadow:0 18px 50px rgb(0 0 0/.24),0 2px 8px rgb(0 0 0/.08);visibility:hidden;transform:translateX(calc(100% + 16px))}} body.native:not(.outline-docked) #outline-side{{padding-top:40px}}
body:not(.outline-docked):not(.obsidian-tree) #outline-side{{background:rgb(var(--bg-sidebar)/.96);backdrop-filter:blur(24px) saturate(1.3);-webkit-backdrop-filter:blur(24px) saturate(1.3)}}
@media(prefers-reduced-transparency:reduce){{body:not(.outline-docked):not(.obsidian-tree) #outline-side{{background:rgb(var(--bg-sidebar));backdrop-filter:none;-webkit-backdrop-filter:none}}}}
body.outline-out:not(.outline-docked) #outline-side{{visibility:visible;transform:none}}
body:not(.outline-docked) #outline-side{{transition:transform .13s cubic-bezier(.4,0,1,1),visibility 0s linear .13s}} body.outline-out:not(.outline-docked) #outline-side{{transition:transform .15s cubic-bezier(.2,.8,.2,1),visibility 0s}} body.outline-still #outline-side{{transition:none!important}}
@media(prefers-reduced-motion:reduce){{body:not(.outline-docked) #outline-side{{transform:none;opacity:0;transition:opacity .15s linear,visibility 0s linear .15s}} body.outline-out:not(.outline-docked) #outline-side{{opacity:1;transition:opacity .15s linear,visibility 0s}} #outline .tw svg{{transition:none}}}}
/* The panel's width, like the sidebar's: drag its left edge, docked or floating (the grip lies inside the edge, since
   the aside clips), double-click to put the default back. While dragging, nothing glides and the reader's iframe can't
   swallow the pointer. Remembered across pages and views (askw:vault:outline-w, inline on .shell). */
#outline-grip{{position:absolute;top:0;left:0;bottom:0;z-index:3;width:8px;cursor:col-resize;touch-action:none}}
#outline-grip::after{{content:"";position:absolute;top:0;left:1px;bottom:0;width:2px;border-radius:2px;background:rgb(var(--accent));opacity:0;transition:opacity .1s}}
body:not(.outline-docked) #outline-grip::after{{top:10px;bottom:10px;left:2px}}
#outline-grip:hover::after,body.outline-resizing #outline-grip::after{{opacity:.7}}
body.outline-resizing,body.outline-resizing *{{cursor:col-resize!important;user-select:none;-webkit-user-select:none}} body.outline-resizing .shell,body.outline-resizing #outline-side{{transition:none!important}} body.outline-resizing #reader{{pointer-events:none}}
/* Page Only (⌘B, the script's `page only` mark): the reader is the whole window. The panels go whether pinned or not,
   with the edge and the toggle that bring them out; the pins stay as they were. In and out at once, with no glide. */
body.page-only .shell{{grid-template-columns:minmax(0,1fr)!important;grid-template-rows:minmax(0,1fr)!important}} body.page-only #reader-pane{{grid-template-rows:0 minmax(0,1fr)!important}}
body.page-only #vault-side,body.page-only #outline-side,body.page-only #side-edge,body.page-only #tab-bar,body.page-only .outline-toggle{{display:none!important}}
body.page-still .shell,body.page-still #reader-pane{{transition:none!important}}
{panels_css}
{search_css}
{find_css}
{tabs_css}
@media(prefers-reduced-motion:no-preference){{body{{transition:background-color 150ms,color 150ms}}#vault-side{{transition:background-color 150ms,color 150ms}}}}
</style><style id=sidebar-theme>{sidebar_css}</style><style id=vault-look>{look_css}</style></head><body class="{body_class}"><div class=shell><aside id=vault-side data-drag><div class=brand><img class=mark src=/onyx-mark.png alt=""><span class=brand-name>{vault_name}</span>{add_toggle}<button id=side-pin class=side-toggle type=button aria-pressed=true title="Unpin sidebar (⌘\\)" aria-label="Pin sidebar" aria-controls=vault-side>{PIN_ICON}</button></div>
<nav class=vault-switch aria-label="Library and vaults"><a href="/"{library_active} data-kind=library>Library</a><a href="/vault"{notes_active} data-kind=notes>Notes</a><a href="/vault?vault=html"{html_active} data-kind=html>Artifacts</a></nav>
{add_panel}
<input id=vault-filter type=search placeholder="Filter {units}… (press /)" autocomplete=off spellcheck=false aria-label="Filter {units}">
<nav id=tree data-nodrag aria-label="{tree_label}"><div class=none>Loading…</div></nav>
<div class=aside-foot><span id=vault-count>v{version}</span><button id=open-history class=foot-btn type=button title="Recent conversations (⌘Y)" aria-label="Recent conversations">{history_icon}</button><button id=open-settings class=foot-btn type=button title="Settings (⌘,)" aria-label="Settings">{settings_icon}</button></div><div id=side-grip data-nodrag role=separator aria-orientation=vertical aria-label="Resize sidebar" title="Drag to resize · double-click to reset"></div></aside>
<main id=reader-pane>{tabs_html}<div id=stage><div id=reader-empty{empty_hidden}><div><span id=empty-hint>{empty_hint}</span><br><small>Select any passage inside it to ask.</small></div></div>
<section id=home data-drag aria-label="Library"{home_hidden}><div class=home-inner>
<form id=open-form class=open-row><input id=open-src placeholder="Open a file or URL — HTML, Markdown, text, PDF, or https://…" spellcheck=false autocomplete=off aria-label="Document URL or local file"><button type=button class="secondary pick" data-pick=file data-target=open-src>Choose…</button><button class=primary>Open</button></form>
<details class=open-context data-nodrag><summary>Context folder: <span id=open-folder-label>{short_folder}</span></summary><div class=row><input id=open-folder value="{default_folder}" spellcheck=false autocomplete=off aria-label="Context folder"><button type=button class="secondary pick" data-pick=folder data-target=open-folder>Choose…</button></div><p class=field-help>Only files inside this folder are available to the provider as evidence. Notes and Artifacts bring their own.</p></details>
<div class=home-h><h2>Recently opened</h2></div><div id=home-docs class=home-grid></div>
<div class=home-h><h2>Recent asks</h2><button type=button id=home-all class=link>See all</button></div><div id=home-asks class=home-list></div>
</div></section>
<iframe id=reader name=reader src="{initial}" aria-label="Reader"></iframe><button id=outline-toggle class=outline-toggle type=button title="Outline and related (⌘⇧\\)" aria-label="Show outline and related pages" aria-controls=outline-side aria-expanded=false>{OUTLINE_ICON}</button>{find_html}</div></main>
<aside id=outline-side data-drag aria-label="Outline and related pages"><div class=brand><div class=pane-tabs role=tablist aria-label="Panel"><button id=tab-outline class=pane-tab type=button role=tab aria-selected=true aria-controls=outline>Outline</button><button id=tab-related class=pane-tab type=button role=tab aria-selected=false aria-controls=related>Related</button></div><button id=outline-fold class=side-toggle type=button title="Collapse all" aria-label="Collapse all headings">{FOLD_ICONS}</button><button id=outline-pin class=side-toggle type=button aria-pressed=false title="Pin panel (⌘⇧\\)" aria-label="Pin panel" aria-controls=outline-side>{PIN_ICON}</button></div>
<input id=outline-filter type=search placeholder="Filter headings…" autocomplete=off spellcheck=false aria-label="Filter headings">
<nav id=outline data-nodrag role=tabpanel aria-labelledby=tab-outline aria-label="Page outline"><div class=none>Open a page to see its outline.</div></nav>
<div id=related data-nodrag role=tabpanel aria-labelledby=tab-related aria-label="Related pages" hidden><svg id=rel-map viewBox="0 0 100 100" aria-hidden=true></svg><div id=rel-list></div></div><div id=outline-grip data-nodrag role=separator aria-orientation=vertical aria-label="Resize panel" title="Drag to resize · double-click to reset"></div></aside></div><div id=side-edge aria-hidden=true></div><div id=peek role=tooltip hidden></div>
{panels_html}
{search_html}
<script src=/app-menu.js></script>
<script>
let KIND={json.dumps(kind)}; const TOKEN={token}; const INITIAL_SRC={initial_src}; const $=s=>document.querySelector(s); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const native=!!(window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.askwPick); if(native)document.body.classList.add('native');
// MARK: moving the window — a WebView swallows the mouse, so AppKit drags the window only by the thin band of title
// bar over the page, and the chrome around the reader looks draggable but is not. Every mouse-down inside [data-drag]
// is handed back to the app, which drags the window by it: the sidebar's top strip, the panel heads, the space around
// the filters and the home page. The lists say [data-nodrag] and the controls keep their own click, as cxtasks and
// cxmail scope `data-tauri-drag-region`.
// The title band is the other way in: a Mac window drags by its top strip whatever is drawn there, unless a control is
// under the pointer. With a page open the reader fills that strip, and a mouse-down inside the frame never reaches this
// document, so the window moved only by its outermost pixel. The band is the top 28 px (the title bar's height) across
// the shell and every reader frame (readerDragDown, bound by onReaderLoad), minus any control there: the page's own,
// the widget's (.askw-root: the context pill sits in the band), and a dialog's. A double-click there is a title bar's
// too, and the app does what Desktop & Dock says (zoom, by default); a pinned tab bar is title bar all the way down.
const DRAG_KEEP='a,button,input,textarea,select,summary,label,[contenteditable],[draggable=true],[data-nodrag]';
const DRAG_BAND=28,BAND_KEEP=DRAG_KEEP+',dialog,[role=search],[role=button],[role=tab],[role=link],[role=menuitem],video,audio,canvas,.askw-root';
function dragWindow(double){{Promise.resolve(window.webkit.messageHandlers.askwDrag.postMessage(double?{{double:true}}:{{}})).catch(()=>{{}})}}
// Where a press lands: 2 in the title band (or on a pinned tab bar), 1 elsewhere in the chrome, 0 on a control or the page.
// A frame's band is measured from the window's top, so under a pinned tab bar the reader has none.
function shellDragAt(e){{const t=e.target;if(!(t instanceof Element))return 0;
if(t.closest('[data-drag]')?t.closest(DRAG_KEEP):e.clientY>=DRAG_BAND||t.closest(BAND_KEEP))return 0;return e.clientY<DRAG_BAND||t.closest('#tab-bar')?2:1}}
function readerDragAt(e){{const t=e.target,f=e.view&&e.view.frameElement;
return f&&t&&t.closest&&f.getBoundingClientRect().top+e.clientY<DRAG_BAND&&!t.closest(BAND_KEEP)?2:0}}
// As Tauri's drag.js does it on macOS, for cxtasks and cxmail: a press drags, and a double-click in the title band is sent
// on its mouse-up, only if the pointer has not moved. The press is kept from the page, so it starts no selection and
// drops none, and a double-click selects no word.
let dragDbl=null;
function dragDown(e,at){{if(pageOnly())return;const where=at(e);if(e.button!==0||!where)return;e.preventDefault();
if(e.detail===1)dragWindow();else if(e.detail===2&&where===2)dragDbl=[e.view,e.clientX,e.clientY]}}
function dragUp(e,at){{const d=dragDbl;dragDbl=null;
if(e.button===0&&e.detail===2&&d&&d[0]===e.view&&d[1]===e.clientX&&d[2]===e.clientY&&at(e)===2)dragWindow(true)}}
if(native){{document.addEventListener('mousedown',e=>dragDown(e,shellDragAt));document.addEventListener('mouseup',e=>dragUp(e,shellDragAt))}}
function readerDragDown(e){{dragDown(e,readerDragAt)}} function readerDragUp(e){{dragUp(e,readerDragAt)}}
{glass_js}
// What each view calls itself; a switch (switchVault, below) moves KIND and everything that hangs off it in place. Library
// is both vaults at once, each tree under its own heading, so every row says which vault it belongs to (data-vault).
const VAULTS={vaults_json},GROUPS={groups_json}; let BOTH=['notes','html'],EXTRA=[],HTML,KEY,UNIT,VAULT;
// The other notes vaults (Settings ▸ Vaults): each a tree of its own, keyed v-<folder>, listed under Notes beneath its own
// heading and in Library between Notes and Artifacts. BOTH is every tree, in that order. viewOf(k) is the view that
// shows vault k; shownIn(view) the trees a view shows. SCOPE is what ⌘P searches unless its switch says otherwise.
let SCOPE={scope_json};
function setExtra(list){{EXTRA=list.map(v=>v.key);for(const v of list)GROUPS[v.key]=v.label;BOTH=['notes',...EXTRA,'html'];for(const k in TREES)if(!BOTH.includes(k))delete TREES[k]}}
function viewOf(k){{return k==='html'||k==='library'?k:'notes'}}
function shownIn(k){{return k==='library'?BOTH:k==='notes'?['notes',...EXTRA]:[k]}}
function keyOf(k){{return 'askw:vault:'+(k==='notes'?'':k+':')}}
function setKind(k){{KIND=k;HTML=k==='html';KEY=keyOf(k);VAULT=VAULTS[k];UNIT=VAULT.unit}} setKind(KIND);
const tree=$('#tree'),filter=$('#vault-filter'),empty=$('#reader-empty'),home=$('#home'),stage=$('#stage'); const TREES={{}}; setExtra({extra_json});
// The reader is the frame of the tab showing (tabs_ui.py): a tab switch moves it, so it is read at the moment it is used.
let reader=$('#reader');
// MARK: reader navigation — instant, as in Obsidian: no fade and no wait. What made a page change feel jerky was what
// changed AFTER the page had painted, and each is now settled before its first frame: the vault look and the reading
// position come in the page itself (app.py `_first_paint`), and a switch resizes the sidebar at once (kind-still).
// `navigate` is how the shell moves the reader; the home page and the empty hint go at the same moment.
const stillMotion=matchMedia('(prefers-reduced-motion:reduce)');
function showHome(show){{if(show===!home.hidden)return;if(show)loadHome();home.hidden=!show}}
function navigate(href){{previewPageLook(href);showHome(false);empty.hidden=true;reader.src=href}}
function store(k,v){{try{{localStorage.setItem(k,v)}}catch(e){{}}}} function recall(k){{try{{return localStorage.getItem(k)}}catch(e){{return null}}}}
// Notes remember which folders are OPEN (default shut, the vault is large); Artifacts remembers which are CLOSED (default
// open, so a project reads at a glance). Library shows those same two trees, and remembers which of its headings is shut.
const FOLDS={{notes:new Set(),html:new Set(),library:new Set()}};
function foldKey(k){{return keyOf(k)+(k==='notes'?'open':'closed')}} for(const k in FOLDS){{try{{for(const p of JSON.parse(recall(foldKey(k))||'[]'))FOLDS[k].add(p)}}catch(e){{}}}}
function isOpen(k,path){{k=viewOf(k);return k==='notes'?FOLDS.notes.has(path):!FOLDS[k].has(path)}}
function setOpen(k,path,open){{k=viewOf(k);const f=FOLDS[k];if(k==='notes'?open:!open)f.add(path);else f.delete(path);store(foldKey(k),JSON.stringify([...f]))}}
async function api(url){{const r=await fetch(url);const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${{r.status}}`);return d}}
function rootOf(k){{const d=TREES[k];return d&&!d.error?d.root:''}}
function viewHref(path,k){{const r=k!=='html'&&rootOf(k);return '/view?src='+encodeURIComponent(path)+(r?'&folder='+encodeURIComponent(r):'')}}
function ago(ts){{if(!ts)return'';const d=Math.max(0,Date.now()/1000-ts);if(d<3600)return Math.max(1,Math.floor(d/60))+'m';if(d<86400)return Math.floor(d/3600)+'h';if(d<86400*14)return Math.floor(d/86400)+'d';if(d<86400*120)return Math.floor(d/604800)+'w';return new Date(ts*1000).toLocaleDateString(undefined,{{month:'short',year:'2-digit'}})}}
function label(n,k){{return k==='html'?(n.title||n.name):n.name.replace(/\\.(md|markdown)$/i,'')}} function badge(ext){{return /^\\.(md|markdown)$/i.test(ext)?'':`<span class=ext>${{esc(ext.replace('.',''))}}</span>`}}
// Rows carry only a label and their vault; what a page is (title, summary, folder, kind, age) waits in NODES for the hover card.
const ICON={icons_json}; const NODES=new Map();
// Artifacts: a row the vault owns (n.entry) drags to another of its folders, and a folder of its own (not linked) takes
// the drop. Everything inside a linked folder is another tree's, so it stays put (vault.owned_entry).
function grip(n,k){{if(k!=='html')return '';return n.entry?` draggable=true data-entry="${{esc(n.entry)}}"${{n.pinned?' data-pinned':''}}`:' draggable=false'}}
function pinMark(n){{return n.pinned?`<span class=pinned title="Pinned to the top">${{ICON.pin}}</span>`:''}}
function fileRow(n,crumbs,k){{NODES.set(n.path,{{n,crumbs,k}});if(n.missing)return `<li><span class="file missing" data-path="${{esc(n.path)}}" data-vault=${{k}} tabindex=0${{grip(n,k)}}><span class=lbl>${{esc(label(n,k))}}</span><span class=ext>missing</span></span></li>`;return `<li><a class=file target=reader href="${{esc(viewHref(n.path,k))}}" data-path="${{esc(n.path)}}" data-vault=${{k}}${{grip(n,k)}}><span class=lbl>${{esc(label(n,k))}}</span>${{pinMark(n)}}${{k==='html'?'':badge(n.ext||'')}}</a></li>`}}
function dirRow(n,crumbs,k){{const inside=crumbs.concat(n.name),own=k==='html'&&!n.linked;return `<li><details data-path="${{esc(n.path)}}" data-vault=${{k}}${{own?` data-rel="${{esc(n.rel)}}"`:''}}${{isOpen(k,n.path)?' open':''}}><summary title="${{esc(n.path)}}"${{grip(n,k)}}>${{ICON.chev}}<span class=fold>${{ICON.shut}}${{ICON.open}}</span><span class=lbl>${{esc(n.name)}}</span>${{pinMark(n)}}${{n.symlink?'<span class=sym title="Linked folder">↗</span>':''}}</summary><ul>${{n.children.map(c=>render(c,inside,k)).join('')}}</ul></details></li>`}}
// Artifacts lists its own folders even while empty (somewhere to drop a page), a linked one only once a page sits beneath it.
function shows(n,k){{return n.kind!=='dir'||k!=='html'||!n.linked||n.children.some(c=>shows(c,k))}}
function render(n,crumbs,k){{return n.kind!=='dir'?fileRow(n,crumbs,k):shows(n,k)?dirRow(n,crumbs,k):''}}
function topRows(k){{return TREES[k].tree.children.filter(c=>shows(c,k)).map(c=>render(c,[],k)).join('')}}
// Library: each vault under its own heading; a vault not set up says so under its heading rather than vanishing.
function groupRow(k){{const d=TREES[k];let body='';if(d&&d.error)body=`<li class=none>${{esc(d.error)}} <a href="#settings" data-settings=set-vaults>Open Settings</a></li>`;else if(d)body=topRows(k)||`<li class=none>${{k==='html'?'No artifacts yet.':'No notes found.'}}</li>`;return `<li class=group><details data-group=${{k}}${{isOpen('library',k)?' open':''}}><summary class=group-head><span class=lbl>${{GROUPS[k]}}</span>${{d&&!d.error?`<span class=count>${{d.files}}</span>`:''}}</summary><ul>${{body}}</ul></details></li>`}}
function renderTree(){{hidePeek();NODES.clear();if(KIND==='library')tree.innerHTML='<ul class=root>'+BOTH.map(groupRow).join('')+'</ul>';else{{const d=TREES[KIND];if(!d||d.error)return;const rows=topRows(KIND)+(KIND==='notes'?EXTRA.filter(k=>TREES[k]).map(groupRow).join(''):'');tree.innerHTML=rows?'<ul class=root>'+rows+'</ul>':`<div class=none>${{HTML?'No artifacts yet. Use + to link pages or a folder of them.':'No notes found.'}}</div>`}}
tree.querySelectorAll('details').forEach(d=>d.addEventListener('toggle',()=>{{if(d.dataset.group)setOpen('library',d.dataset.group,d.open);else setOpen(d.dataset.vault,d.dataset.path,d.open)}}));highlight(currentSrc());applyTints();if(HTML)fillDestinations()}}
function currentSrc(){{try{{const l=reader.contentWindow.location;if(!l||!l.href||l.href==='about:blank')return '';return new URLSearchParams(l.search).get('src')||''}}catch(e){{return ''}}}}
function readerPage(){{try{{const h=reader.contentWindow.location.href;return h&&h!=='about:blank'?h:''}}catch(e){{return ''}}}}
function highlight(src,block){{tree.querySelectorAll('a.active').forEach(a=>a.classList.remove('active'));if(!src)return;const a=tree.querySelector(`a[data-path="${{CSS.escape(src)}}"]`);if(!a)return;a.classList.add('active');let p=a.parentElement;while(p&&p!==tree){{if(p.tagName==='DETAILS'&&!p.open)p.open=true;p=p.parentElement}}a.scrollIntoView({{block:block||'nearest'}})}}
// The tree menu's Reveal and Collapse commands (the menu is further down). Reveal is the same walk the reader's own page
// changes take, asked for by hand and centred: after a Collapse All, or after the filter took the tree somewhere else.
// It is offered only for a page this tree actually holds. Collapse All leaves Library's two headings open — shutting
// those would hide both trees rather than fold them — and the rows' own `toggle` listener remembers the result.
function revealTarget(){{const src=currentSrc();if(!src)return '';const k=vaultOf(src);return k&&(KIND==='library'||viewOf(k)===KIND)?src:''}}
function revealCurrent(){{const src=revealTarget();if(!src)return;if(filter.value){{filter.value='';renderTree()}}highlight(src,'center')}}
function collapsed(){{return !tree.querySelector('details:not([data-group])[open]')}}
function collapseAll(){{tree.querySelectorAll('details:not([data-group])[open]').forEach(d=>{{d.open=false}})}}
// Both vaults' trees are kept (TREES), so a switch shows the other at once; each is fetched again behind it, and the
// list is redrawn only if that brought something new. Library shows the two together.
async function fetchTree(k){{try{{TREES[k]=await api('/api/vault/tree?vault='+k)}}catch(e){{TREES[k]={{error:e.message}}}}return TREES[k]}}
function countText(){{if(KIND==='library')return BOTH.map(k=>{{const d=TREES[k];return d&&!d.error?d.files+' '+VAULTS[viewOf(k)].unit+(d.files===1?'':'s')+(EXTRA.includes(k)?' in '+GROUPS[k]:''):''}}).filter(Boolean).join(' · ')||'No vaults set up';const d=TREES[KIND];return d.files+' '+UNIT+(d.files===1?'':'s')+(d.missing?' · '+d.missing+' missing':'')+(d.truncated?' (truncated)':'')+(KIND==='notes'?EXTRA.map(k=>{{const x=TREES[k];return x&&!x.error?' · '+x.files+' in '+GROUPS[k]:''}}).join(''):'')}}
function showTree(){{if(KIND!=='library'){{const d=TREES[KIND];if(!d)return;if(d.error){{tree.innerHTML=`<div class=none>${{esc(d.error)}} <a href="#settings" data-settings=set-vaults>Open Settings</a></div>`;$('#vault-count').textContent=HTML?'no Artifacts folder':'no vault';return}}}}$('#vault-count').textContent=countText();renderTree()}}
function sameTree(a,b){{return !!a&&!!b&&a.error===b.error&&a.files===b.files&&a.missing===b.missing&&a.truncated===b.truncated&&JSON.stringify(a.tree)===JSON.stringify(b.tree)}}
async function loadTree(){{const k=KIND,ks=shownIn(k),was=ks.map(x=>TREES[x]),got=await Promise.all(ks.map(fetchTree));if(k===KIND&&got.some((d,i)=>!sameTree(was[i],d)))showTree()}}
// After a vault folder changes in Settings: both trees again, and the home page, whose tags come from them.
async function reloadTrees(){{await Promise.all(BOTH.map(fetchTree));showTree();if(!home.hidden)loadHome()}}
// The vault a page lives in: the one whose root it sits under (the deeper, should one hold the other); '' for neither.
function vaultOf(src){{let best='',depth=0;for(const k in TREES){{const r=TREES[k].root;if(r&&r.length>depth&&src.startsWith(r+'/')){{best=k;depth=r.length}}}}return best}}
// MARK: sidebar — pinned or unpinned, remembered across every view ("collapsed" is the old word for unpinned). Unpinned,
// it comes out after a beat on the left edge and goes a moment after the pointer leaves, unless it is in use: a row menu
// open, the + panel open, or typing in one of its fields. ⌘\\ pins and unpins, also while focus is inside the reader.
const SIDE_KEY='askw:vault:sidebar', side=$('#vault-side'), pin=$('#side-pin'), edge=$('#side-edge'); let sideOver=false, sideTimer=0;
function pinned(){{return !document.body.classList.contains('side-unpinned')}}
function sideOut(out){{document.body.classList.toggle('side-out',out);side.inert=!pinned()&&!out;if(!out)hidePeek()}}
function inUse(){{const a=document.activeElement,p=$('#add-panel');return !!(DRAG||gripFrom||(window.OnyxMenu&&OnyxMenu.isOpen())||(p&&!p.hidden)||(a&&side.contains(a)&&/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)))}}
function sideLater(){{clearTimeout(sideTimer);if(pinned())return;sideTimer=setTimeout(function check(){{if(pinned()||sideOver)return;if(inUse()){{sideTimer=setTimeout(check,400);return}}sideOut(false)}},400)}}
function setPinned(on){{document.body.classList.toggle('side-unpinned',!on);store(SIDE_KEY,on?'':'unpinned');pin.setAttribute('aria-pressed',String(on));pin.title=(on?'Unpin':'Pin')+' sidebar (⌘\\\\)';clearTimeout(sideTimer);sideOut(!on&&(sideOver||inUse()));if(!on&&!sideOver)sideLater()}}
function sideKey(e){{if(e.key==='\\\\'&&(e.metaKey||e.ctrlKey)&&!e.altKey&&!e.shiftKey){{e.preventDefault();setPinned(!pinned())}}}}
pin.onclick=()=>setPinned(!pinned()); document.addEventListener('keydown',sideKey);
edge.addEventListener('mouseenter',()=>{{clearTimeout(sideTimer);sideTimer=setTimeout(()=>sideOut(true),40)}}); edge.addEventListener('mouseleave',e=>{{if(side.contains(e.relatedTarget))return;clearTimeout(sideTimer);if(document.body.classList.contains('side-out'))sideLater()}});
side.addEventListener('mouseenter',()=>{{sideOver=true;clearTimeout(sideTimer)}}); side.addEventListener('mouseleave',()=>{{sideOver=false;sideLater()}}); side.addEventListener('focusout',()=>{{if(!sideOver)sideLater()}});
// MARK: sidebar width — drag the grip on its right edge, pinned or floating; double-click puts the kind's default back.
// Pointer capture keeps the drag alive past the edge; inUse() (above) keeps the floating panel out meanwhile.
const WIDE_KEY='askw:vault:side-w', sideGrip=$('#side-grip'), shell=$('.shell'); let gripFrom=null;
function sideWidth(w){{if(w==null){{shell.style.removeProperty('--side-w');store(WIDE_KEY,'');return}}w=Math.round(Math.min(Math.max(w,200),Math.max(200,innerWidth*.5)));shell.style.setProperty('--side-w',w+'px');store(WIDE_KEY,String(w))}}
sideGrip.addEventListener('pointerdown',e=>{{if(e.button!==0)return;e.preventDefault();gripFrom={{x:e.clientX,w:side.getBoundingClientRect().width}};try{{sideGrip.setPointerCapture(e.pointerId)}}catch(err){{}}document.body.classList.add('side-resizing')}});
sideGrip.addEventListener('pointermove',e=>{{if(gripFrom)sideWidth(gripFrom.w+e.clientX-gripFrom.x)}});
function gripEnd(){{if(!gripFrom)return;gripFrom=null;document.body.classList.remove('side-resizing');if(!pinned()&&!sideOver)sideLater()}}
sideGrip.addEventListener('pointerup',gripEnd); sideGrip.addEventListener('pointercancel',gripEnd); sideGrip.addEventListener('lostpointercapture',gripEnd);
sideGrip.addEventListener('dblclick',()=>sideWidth(null));
// Put back as it was left without a glide: the width is set under side-still, lifted once the first frame has painted.
{{const w=parseInt(recall(WIDE_KEY)||'',10);if(w>0){{document.body.classList.add('side-still');sideWidth(w);requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('side-still')))}}}}
// MARK: outline — the page's headings, read out of the reader's document (same origin) and nested by level, as Obsidian's
// outline pane: a row scrolls the page to its heading, the twisty folds a section, the filter keeps matching rows and
// their parents, and the section being read stays marked as the page scrolls. Pinned, it docks as the grid's third
// column; unpinned (the default) it floats out after a beat on the reader's right edge or the round toggle in its corner,
// and goes a moment after the pointer leaves it, unless it is being typed in. The edge is watched from the reader's own
// pointer moves (same origin), not an overlay as on the left, so the page's scrollbar under it stays clickable. ⌘⇧\\ pins and unpins, also from inside the reader; a narrow window never
// docks it. The pin is remembered; a fold is kept while its page stays open.
const OUT_KEY='askw:vault:outline', outSide=$('#outline-side'), outNav=$('#outline'), outPin=$('#outline-pin'), outToggle=$('#outline-toggle'), outFilter=$('#outline-filter'), outFold=$('#outline-fold'), narrow=matchMedia('(max-width:800px)');
let outPinned=recall(OUT_KEY)==='pinned', outOver=false, outTimer=0, outBuildTimer=0, outRaf=0, outObserver=null, outActive=-1, outSig='', outGripFrom=null; let HEADS=[]; const outShut=new Set();
function outDocked(){{return document.body.classList.contains('outline-docked')}} function outShown(){{return outDocked()||document.body.classList.contains('outline-out')}}
function outOut(out){{document.body.classList.toggle('outline-out',out);outSide.inert=!outDocked()&&!out;outToggle.setAttribute('aria-expanded',String(outShown()))}}
function outInUse(){{const a=document.activeElement;return !!(outGripFrom||(a&&outSide.contains(a)&&/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)))}}
function outLater(){{clearTimeout(outTimer);if(outDocked())return;outTimer=setTimeout(function check(){{if(outDocked()||outOver)return;if(outInUse()){{outTimer=setTimeout(check,400);return}}outOut(false)}},400)}}
// The context pill in the reader's corner moves left of the toggle while the toggle is there (ask.js reads the variable),
// and left of the find bar (find_ui.py) while that is open.
function pillRoom(){{try{{const st=reader.contentDocument.documentElement.style,bar=$('#find-bar'),right=(outDocked()?12:50)+(bar.hidden?0:bar.offsetWidth+8);if(right===12)st.removeProperty('--askw-pill-right');else st.setProperty('--askw-pill-right',right+'px')}}catch(e){{}}}}
function applyOutlinePin(){{const dock=outPinned&&!narrow.matches;document.body.classList.toggle('outline-docked',dock);outPin.setAttribute('aria-pressed',String(outPinned));outPin.title=(outPinned?'Unpin':'Pin')+' panel (⌘⇧\\\\)';clearTimeout(outTimer);outOut(!dock&&(outOver||outInUse()));if(!dock&&!outOver)outLater();pillRoom()}}
function setOutlinePinned(on){{outPinned=on;store(OUT_KEY,on?'pinned':'');applyOutlinePin()}}
function outKey(e){{if((e.key==='\\\\'||e.key==='|')&&e.shiftKey&&(e.metaKey||e.ctrlKey)&&!e.altKey){{e.preventDefault();setOutlinePinned(!outPinned)}}}}
outPin.onclick=()=>setOutlinePinned(!outPinned); document.addEventListener('keydown',outKey); narrow.addEventListener('change',applyOutlinePin);
outToggle.addEventListener('mouseenter',()=>{{clearTimeout(outTimer);outTimer=setTimeout(()=>outOut(true),40)}}); outToggle.addEventListener('mouseleave',e=>{{if(outSide.contains(e.relatedTarget))return;clearTimeout(outTimer);if(document.body.classList.contains('outline-out'))outLater()}});
outToggle.onclick=()=>{{clearTimeout(outTimer);outOut(true)}};
let outAtEdge=false; function outEdgeMove(e){{const w=e.view||window,at=!outDocked()&&!pageOnly()&&w.innerWidth-e.clientX<=24;if(at===outAtEdge)return;outAtEdge=at;clearTimeout(outTimer);if(at)outTimer=setTimeout(()=>outOut(true),40);else if(document.body.classList.contains('outline-out'))outLater()}}
outSide.addEventListener('mouseenter',()=>{{outOver=true;clearTimeout(outTimer)}}); outSide.addEventListener('mouseleave',()=>{{outOver=false;outLater()}}); outSide.addEventListener('focusout',()=>{{if(!outOver)outLater()}});
outSide.addEventListener('keydown',e=>{{if(e.key!=='Escape')return;if(document.activeElement===outFilter&&outFilter.value){{outFilter.value='';applyOutFilter()}}else{{document.activeElement.blur();if(!outDocked())outOut(false)}}}});
// MARK: panel width — the grip on its left edge, dragged docked or floating, as the sidebar's is on the right;
// double-click puts the default back. Pointer capture keeps the drag alive past the edge, and outInUse()
// (above) keeps the floating panel out meanwhile. One width across Library, Notes and Artifacts, remembered.
const OUT_WIDE_KEY='askw:vault:outline-w', outGrip=$('#outline-grip');
function outWidth(w){{if(w==null){{shell.style.removeProperty('--outline-w');store(OUT_WIDE_KEY,'');return}}w=Math.round(Math.min(Math.max(w,200),Math.max(200,innerWidth*.5)));shell.style.setProperty('--outline-w',w+'px');store(OUT_WIDE_KEY,String(w))}}
outGrip.addEventListener('pointerdown',e=>{{if(e.button!==0)return;e.preventDefault();outGripFrom={{x:e.clientX,w:outSide.getBoundingClientRect().width}};try{{outGrip.setPointerCapture(e.pointerId)}}catch(err){{}}document.body.classList.add('outline-resizing')}});
outGrip.addEventListener('pointermove',e=>{{if(outGripFrom)outWidth(outGripFrom.w+outGripFrom.x-e.clientX)}});
function outGripEnd(){{if(!outGripFrom)return;outGripFrom=null;document.body.classList.remove('outline-resizing');if(!outDocked()&&!outOver)outLater()}}
outGrip.addEventListener('pointerup',outGripEnd); outGrip.addEventListener('pointercancel',outGripEnd); outGrip.addEventListener('lostpointercapture',outGripEnd);
// The panel never scrolls sideways: its grip lies inside the edge it pulls. WebKit measures the panel's content 6 px
// wider than its box and scrolled it by that on a click on its pin, which slid the grip out under the reader (2026-09-25).
outSide.addEventListener('scroll',()=>{{if(outSide.scrollLeft)outSide.scrollLeft=0}},{{passive:true}});
outGrip.addEventListener('dblclick',()=>outWidth(null));
// Put back as it was left: set before the first paint (the page opens under outline-still), so nothing glides into place.
{{const w=parseInt(recall(OUT_WIDE_KEY)||'',10);if(w>0)outWidth(w)}}
// The reader's document, while it holds a page (about:blank and the home page have no outline).
function readerDoc(){{try{{const d=reader.contentDocument;return d&&d.body&&readerPage()?d:null}}catch(e){{return null}}}}
// Every heading in the page's own content: not the widget's (answers carry headings of their own), not a page's nav rail,
// and not one hidden away — except inside a shut <details>, which a click opens on the way to it.
// While ⌘E has the note open for editing (ask.js, static/onyx-editor.js), the page's own headings are hidden under the
// editor, which answers for them: each with where it is and a way there, as an element would.
function editorOf(doc){{try{{const e=doc&&doc.defaultView&&doc.defaultView.askwEditor;return e&&e.live()?e:null}}catch(e){{return null}}}}
function headingsOf(doc){{const ed=editorOf(doc);if(ed)return ed.headings();const out=[];for(const el of doc.querySelectorAll('h1,h2,h3,h4,h5,h6')){{if(el.closest('.askw-root,nav,[hidden],[aria-hidden=true]'))continue;const text=(el.textContent||'').replace(/\\s+/g,' ').trim();if(!text)continue;const r=el.getBoundingClientRect();if(!r.width&&!r.height&&!el.closest('details'))continue;out.push({{el,level:+el.tagName[1],text}})}}return out}}
function outlineTree(list){{const root={{kids:[]}},stack=[{{level:0,node:root}}];list.forEach((h,i)=>{{const node={{i,h,kids:[]}};while(stack.length>1&&stack[stack.length-1].level>=h.level)stack.pop();stack[stack.length-1].node.kids.push(node);stack.push({{level:h.level,node}})}});return root.kids}}
function outRow(n){{const k=n.h.level+':'+n.h.text,kids=n.kids.length>0,shut=kids&&outShut.has(k);return `<li class="${{kids?'has-kids':''}}${{shut?' shut':''}}" data-k="${{esc(k)}}"><div class=row><button class=tw type=button tabindex=-1 aria-label="${{shut?'Expand':'Collapse'}}" aria-expanded=${{!shut}}>${{ICON.chev}}</button><button class=h type=button data-i=${{n.i}} title="${{esc(n.h.text)}}">${{esc(n.h.text)}}</button></div>${{kids?'<ul>'+n.kids.map(outRow).join('')+'</ul>':''}}</li>`}}
function foldState(){{const open=outNav.querySelector('li.has-kids:not(.shut)');outFold.classList.toggle('all-shut',!open&&!!outNav.querySelector('li.has-kids'));const label=open?'Collapse all':'Expand all';outFold.title=label;outFold.setAttribute('aria-label',label+' headings')}}
// Redrawn only when the headings themselves change (a page's script, or an answer, may add to the document later), so a
// fold survives; a redraw that finds the same headings just picks up their new elements.
function buildOutline(){{const doc=readerDoc(),list=doc?headingsOf(doc):[],sig=(doc?'page\\n':'')+list.map(h=>h.level+h.text).join('\\n');HEADS=list;
if(sig!==outSig){{outSig=sig;outActive=-1;outNav.innerHTML=!doc?'<div class=none>Open a page to see its outline.</div>':!list.length?'<div class=none>No headings on this page.</div>':'<ul class=root>'+outlineTree(list).map(outRow).join('')+'</ul>';applyOutFilter();foldState()}}trackOutline()}}
function watchReader(){{if(outObserver)outObserver.disconnect();outObserver=null;const doc=readerDoc();if(!doc)return;outObserver=new MutationObserver(()=>{{clearTimeout(outBuildTimer);outBuildTimer=setTimeout(buildOutline,200)}});outObserver.observe(doc.body,{{childList:true,subtree:true,attributeFilter:['open','hidden']}})}}
// The section being read: the last heading at or above the top of the reader (a beat below it) — the first while none
// is yet — or the last of all once the page is scrolled to its end, where a short last section never reaches the top.
function trackOutline(){{const doc=readerDoc();if(!doc||!HEADS.length){{setOutActive(-1);return}}const win=doc.defaultView,end=win.scrollY+win.innerHeight>=doc.documentElement.scrollHeight-2;let a=0;if(end)a=HEADS.length-1;else for(let i=0;i<HEADS.length;i++){{const r=HEADS[i].el.getBoundingClientRect();if(!r.width&&!r.height)continue;if(r.top<=80)a=i;else break}}setOutActive(a)}}
function outScrolled(){{if(outRaf)return;outRaf=requestAnimationFrame(()=>{{outRaf=0;trackOutline()}})}}
function setOutActive(i){{if(i===outActive)return;outActive=i;outNav.querySelectorAll('.h.active').forEach(b=>b.classList.remove('active'));const b=i>=0?outNav.querySelector(`.h[data-i="${{i}}"]`):null;if(!b)return;b.classList.add('active');if(!outOver)b.scrollIntoView({{block:'nearest'}})}}
function goHeading(i){{const h=HEADS[i];if(!h)return;for(let d=h.el.closest('details');d;d=d.parentElement&&d.parentElement.closest('details'))d.open=true;h.el.scrollIntoView({{block:'start',behavior:stillMotion.matches?'auto':'smooth'}});setOutActive(i)}}
function setShut(li,shut){{li.classList.toggle('shut',shut);const tw=li.querySelector(':scope > .row .tw');tw.setAttribute('aria-expanded',String(!shut));tw.setAttribute('aria-label',shut?'Expand':'Collapse');if(shut)outShut.add(li.dataset.k);else outShut.delete(li.dataset.k)}}
outNav.addEventListener('click',e=>{{const tw=e.target.closest('.tw');if(tw){{setShut(tw.closest('li'),!tw.closest('li').classList.contains('shut'));foldState();return}}const b=e.target.closest('.h');if(b)goHeading(+b.dataset.i)}});
outFold.onclick=()=>{{const shut=!!outNav.querySelector('li.has-kids:not(.shut)');outNav.querySelectorAll('li.has-kids').forEach(li=>setShut(li,shut));foldState()}};
// The filter keeps a row that matches, and every row above it, with folds opened for the look.
function applyOutFilter(){{const q=outFilter.value.trim().toLowerCase();outNav.classList.toggle('filtering',!!q);const lis=[...outNav.querySelectorAll('li')];lis.forEach(li=>li.classList.remove('miss'));if(!q)return;
for(const li of lis.reverse()){{const own=li.querySelector(':scope > .row .h').textContent.toLowerCase().includes(q),kid=li.querySelector(':scope > ul > li:not(.miss)');li.classList.toggle('miss',!own&&!kid)}}}}
let outFilterTimer; outFilter.oninput=()=>{{clearTimeout(outFilterTimer);outFilterTimer=setTimeout(applyOutFilter,120)}};
// A page just loaded: its folds start open, its headings are read once the widget has drawn, and followed from then on.
function outlineLoaded(){{try{{const w=reader.contentWindow;w.addEventListener('keydown',outKey);w.addEventListener('scroll',outScrolled,{{passive:true}});w.addEventListener('resize',outScrolled);w.addEventListener('mousemove',outEdgeMove,{{passive:true}})}}catch(e){{}}outAtEdge=false;outShut.clear();outSig='';outFilter.value='';watchReader();buildOutline();relPageChanged();pillRoom()}}
// MARK: related — the pages nearest the one being read (/api/related, search.py): the index ⌘P searches, read from a
// page's own direction instead of from a query. Nothing is embedded for it, so it answers while Ollama is off. The map
// above the list draws the neighbourhood (relLayout, below). Only pages past a floor are shown at all, so everything
// here is related by design and nothing needs dimming or excusing: a page with no neighbour shows none rather than
// twenty of its own tail.
const PANE_KEY='askw:vault:pane', relPane=$('#related'), relList=$('#rel-list'), relMap=$('#rel-map'), tabOut=$('#tab-outline'), tabRel=$('#tab-related');
let relOn=recall(PANE_KEY)==='related', relFor=null, relSeq=0, relRows=[];
let relFloor=0.45;  // until the first answer says; the server owns it
function setPane(on){{relOn=on;store(PANE_KEY,on?'related':'');tabOut.setAttribute('aria-selected',String(!on));tabRel.setAttribute('aria-selected',String(on));
outNav.hidden=on;outFilter.hidden=on;outFold.hidden=on;relPane.hidden=!on;if(on)loadRelated()}}
tabOut.onclick=()=>setPane(false); tabRel.onclick=()=>setPane(true);
// A new page in the reader: what was worked out for the last one no longer describes it, and the scan is worth its
// 200 ms only while the pane is the one showing.
function relPageChanged(){{relFor=null;relSeq++;if(relOn)loadRelated()}}
function relHere(){{const src=currentSrc();return src?{{path:src,vault:vaultOf(src)||(KIND==='library'?'notes':KIND)}}:null}}
async function loadRelated(){{const here=relHere(),seq=++relSeq;
if(!here){{relFor=null;drawRelated({{items:[],reason:'Open a page to see what it sits near.'}});return}}
if(relFor===here.path)return;
relPane.classList.add('no-map');relList.innerHTML='<div class=none>Looking…</div>';
let d;try{{d=await api('/api/related?vault='+here.vault+'&path='+encodeURIComponent(here.path))}}catch(e){{d={{items:[],reason:e.message}}}}
if(seq!==relSeq||d.superseded)return;relFor=here.path;drawRelated(d)}}
// The map is a radial stress layout (Brandes & Pich, "More Flexible Radial Layout", 2011). A dot's distance from the
// centre is its score, stretched over this page's own neighbours: the nearest on the inner ring, the furthest on the
// outer, so the disc fills whatever band they score in. On one scale for every page they drew as a ring, since a
// page's neighbours score within a few hundredths of each other. The angle carries the rest of the meaning: each dot
// turns round its ring until the distances between dots match how related the pages are to one another (`near`, from
// search.py), so neighbours on one subject sit together. Measured over 80 of the author's pages, the drawn distances
// rank-agree exactly with the scores and 0.65 with those relations; Smart Connections' layout, four k-means corners
// pulled on by forces, manages 0.36, and angles picked at random 0.14.
const REL_MAP=12, REL_IN=9, REL_OUT=45, REL_SPREAD=.6;
// What one dot occupies: the dot, and its score printed on its outward side — above it in the map's top half, below
// it in the bottom half — so no score ever points at the page in the middle. Two dots whose boxes meet get pushed apart.
const REL_BOX_W=11, REL_LAB=8.5, REL_DOT=3.5, REL_HERE=4.5;
function relRings(scores){{const lo=Math.min(...scores),hi=Math.max(...scores);
return scores.map(s=>REL_IN+(1-(hi>lo?(s-lo)/(hi-lo):.5))*(REL_OUT-REL_IN))}}
// Where each dot starts round its ring: its angle in a classical MDS of the neighbours alone, so the stress pass begins
// near its answer. Power iteration from a fixed vector, so a page draws the same way every time it opens.
function relStart(D){{const n=D.length,sq=D.map(r=>r.map(d=>d*d)),rm=sq.map(r=>r.reduce((a,b)=>a+b,0)/n),gm=rm.reduce((a,b)=>a+b,0)/n,axes=[];
let B=sq.map((r,i)=>r.map((d,j)=>-.5*(d-rm[i]-rm[j]+gm)));
for(let k=0;k<2;k++){{let v=D.map((_,i)=>Math.sin(1+2.4*i+k)),lam=0;
for(let it=0;it<200;it++){{const w=B.map(r=>r.reduce((a,b,j)=>a+b*v[j],0));lam=Math.hypot(...w)||1e-12;v=w.map(x=>x/lam)}}
lam=v.reduce((a,x,i)=>a+x*B[i].reduce((t,b,j)=>t+b*v[j],0),0);axes.push(v.map(x=>x*Math.sqrt(Math.max(lam,1e-12))));
B=B.map((r,i)=>r.map((b,j)=>b-lam*v[i]*v[j]))}}
const mx=axes[0].reduce((a,b)=>a+b,0)/n,my=axes[1].reduce((a,b)=>a+b,0)/n;return D.map((_,i)=>Math.atan2(axes[1][i]-my,axes[0][i]-mx))}}
// Stress majorization with each dot held to its ring: a dot moves to where its distances to the others best match
// their targets, then back onto its ring. A pair's target is stretched over this page's pairs as the rings are over
// its scores: the most related pair shares an angle, as close as their rings let them be; the least related pair sits
// on opposite sides of the centre; the rest fall between, in order. So the whole disc is used whatever band the
// relations fall in. Fitting one scale to the layout instead let a page whose neighbours were all alike shrink into
// one corner of the disc, a feedback that rank agreement alone did not see.
//
// "In order" leans outward by REL_SPREAD, a power under 1, because the two things the map is for pull against each
// other. Measured over 80 pages: stretched straight (power 1) the dots rank-agree 0.69 with the relations but the
// median page leaves a 144° wedge of the disc empty; at 0.6 they agree 0.65 and leave 84°, which fills the disc as
// evenly as Smart Connections' map (88°) while showing the relations nearly twice as well (0.36). Below 0.5 the
// agreement falls faster than the disc fills.
function relStress(R,D){{const n=R.length,P=relStart(D).map((a,i)=>[50+R[i]*Math.cos(a),50+R[i]*Math.sin(a)]);let lo=Infinity,hi=-Infinity;
for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){{lo=Math.min(lo,D[i][j]);hi=Math.max(hi,D[i][j])}}
const T=D.map((row,i)=>row.map((d,j)=>{{const t=Math.pow(hi>lo?(d-lo)/(hi-lo):.5,REL_SPREAD),near=Math.abs(R[i]-R[j]);return Math.max(4,near+t*(R[i]+R[j]-near))}}));
for(let it=0;it<300;it++)for(let i=0;i<n;i++){{let x=0,y=0,sum=0;
for(let j=0;j<n;j++){{if(j===i)continue;const t=T[i][j],w=1/(t*t),dx=P[i][0]-P[j][0],dy=P[i][1]-P[j][1],d=Math.hypot(dx,dy)||1e-9;
x+=w*(P[j][0]+t*dx/d);y+=w*(P[j][1]+t*dy/d);sum+=w}}
x/=sum;y/=sum;const l=Math.hypot(x-50,y-50)||1e-9;P[i]=[50+R[i]*(x-50)/l,50+R[i]*(y-50)/l]}}
return P}}
// Stress alone lets two dots, or a dot and a score, land on each other. Each pair that meets is turned apart round its
// own two rings, by as much as they overlap, until every box is clear; so a dot's distance from the centre, its score,
// never moves.
function relBox(p){{return p[1]<50?[p[1]-REL_LAB,p[1]+REL_DOT]:[p[1]-REL_DOT,p[1]+REL_LAB]}}
function relClash(a,b){{const dx=REL_BOX_W-Math.abs(a[0]-b[0]);if(dx<=0)return 0;const s=relBox(a),t=relBox(b),dy=Math.min(s[1],t[1])-Math.max(s[0],t[0]);return dy>0?Math.min(dx,dy):0}}
function relSeparate(P,R){{const n=P.length,ang=P.map(p=>Math.atan2(p[1]-50,p[0]-50)),at=i=>[50+R[i]*Math.cos(ang[i]),50+R[i]*Math.sin(ang[i])];
for(let round=0;round<200;round++){{let moved=false;
for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){{const a=at(i),b=at(j),depth=relClash(a,b);if(!depth)continue;moved=true;
const side=Math.sign((a[0]-50)*(b[1]-50)-(a[1]-50)*(b[0]-50))||1,step=depth/2+.25;ang[i]-=side*step/R[i];ang[j]+=side*step/R[j]}}
if(!moved)break}}
return P.map((_,i)=>at(i))}}
// Where each of a page's first REL_MAP neighbours goes, from their scores and how related they are to one another.
function relLayout(scores,near){{const n=scores.length,R=relRings(scores);if(n===1)return [[50,50-R[0]]];
const D=scores.map((_,i)=>scores.map((_,j)=>i===j?0:Math.max(.001,1-((near[i]||[])[j]??.5))));
return relSeparate(relStress(R,D),R)}}
function relWhere(r){{return [GROUPS[r.vault]].concat(r.folder?r.folder.split('/'):[]).join(' › ')}}
// The number shown is the score relabelled onto the range Smart Connections' readers know, where a good match reads
// 0.7 to 0.9: the floor shows as REL_SHOW_FLOOR, the same text as 1, straight between, so the order never changes.
// The score itself has the vault's common direction taken out (search.py), which is what keeps meeting notes from
// neighbouring everything, but it puts a strong match at 0.5 — the same pair is 0.85 by bare cosine — and asks anyone
// who knows Obsidian's pane to convert in their head. Bare cosine itself can't be shown: measured, it bunches every row
// into 0.86–0.96 and reads 38% of adjacent rows out of order. Nor can the two panes agree note for note, since they
// embed with different models; this puts them in the same range, not on the same number.
const REL_SHOW_FLOOR=.7;
function relShow(s){{return (REL_SHOW_FLOOR+Math.max(0,Math.min(1,(s-relFloor)/Math.max(.05,1-relFloor)))*(1-REL_SHOW_FLOOR)).toFixed(2)}}
function relScoreTitle(s){{return relShow(s)+' — '+REL_SHOW_FLOOR.toFixed(2)+' is the least related page worth listing, 1 the same text'}}
function relDots(items,near){{const n=Math.min(items.length,REL_MAP),P=relLayout(items.slice(0,n).map(r=>r.score),near||[]),dots=[],labs=[];
dots.push(`<circle class=rel-here cx=50 cy=50 r=${{REL_HERE}}><title>${{esc(document.title.replace(/ — [^—]*$/,''))}}</title></circle>`);
P.forEach(([x,y],i)=>{{const r=items[i];
dots.push(`<circle class="rel-dot${{r.dupe?' dupe':''}}" data-i=${{i}} cx=${{x.toFixed(1)}} cy=${{y.toFixed(1)}} r=3><title>${{esc(r.title)}} — ${{relScoreTitle(r.score)}}</title></circle>`);
labs.push(`<text class=rel-lab data-i=${{i}} x=${{x.toFixed(1)}} y=${{(y<50?y-4.6:y+7.8).toFixed(1)}}>${{relShow(r.score)}}</text>`)}});
return dots.concat(labs).join('')}}
function drawRelated(d){{const items=d.items||[];relRows=items;relPane.classList.toggle('no-map',!items.length);
if(typeof d.floor==='number')relFloor=d.floor;
if(!items.length){{relMap.innerHTML='';const why=d.reason?d.reason.charAt(0).toUpperCase()+d.reason.slice(1):'Nothing in the vault is near this page — its subject appears nowhere else';relList.innerHTML='<div class=none>'+esc(why.replace(/\\.?$/,'.'))+'</div>';return}}
relMap.innerHTML=relDots(items,d.near);
relList.innerHTML=items.map((r,i)=>`<button class=rel-row type=button data-i=${{i}} title="${{esc(r.title)}}${{r.section?' — '+esc(r.section):''}}">`
+`<span class=rel-t><span class=rel-score title="${{esc(relScoreTitle(r.score))}}">${{relShow(r.score)}}</span><span class=rel-name>${{esc(r.title)}}</span>${{r.dupe?'<span class=rel-dupe>same?</span>':''}}</span>`
+`<span class=rel-where>${{esc(relWhere(r))}}</span></button>`).join('')}}
function relMark(i){{for(const el of relPane.querySelectorAll('.on'))el.classList.remove('on');if(i<0)return;
const row=relList.querySelector(`.rel-row[data-i="${{i}}"]`);if(row)row.classList.add('on');for(const el of relMap.querySelectorAll(`[data-i="${{i}}"]`))el.classList.add('on')}}
function relOpen(r,inTab){{if(!r)return;const k=KIND!=='library'&&viewOf(r.vault)!==KIND?viewOf(r.vault):KIND;if(inTab){{openTab(viewHref(r.path,r.vault),{{kind:k}});return}}if(k!==KIND)switchVault(k,true);if(currentSrc()!==r.path)navigate(viewHref(r.path,r.vault))}}
relList.addEventListener('mousemove',e=>{{const b=e.target.closest('.rel-row');relMark(b?+b.dataset.i:-1)}});
relMap.addEventListener('mousemove',e=>{{const c=e.target.closest('.rel-dot');relMark(c?+c.dataset.i:-1)}});
relPane.addEventListener('mouseleave',()=>relMark(-1));
relList.addEventListener('focusin',e=>{{const b=e.target.closest('.rel-row');if(b)relMark(+b.dataset.i)}});
relList.addEventListener('click',e=>{{const b=e.target.closest('.rel-row');if(b)relOpen(relRows[+b.dataset.i],e.metaKey||e.ctrlKey)}});
relMap.addEventListener('click',e=>{{const c=e.target.closest('.rel-dot');if(c)relOpen(relRows[+c.dataset.i],e.metaKey||e.ctrlKey)}});
// MARK: reader — what shows in the reader's place, and the window's URL and title, follow whatever the reader loads, and
// whichever tab is brought forward (the URL is always the tab showing, so ⌘R keeps it). History that crosses into the
// other vault (back past a switch) brings the sidebar along; a link inside a page doesn't.
function traversed(f){{try{{const n=(f||reader).contentWindow.performance.getEntriesByType('navigation')[0];return !!n&&n.type==='back_forward'}}catch(e){{return false}}}}
// Library rests on its home page, in the reader's place; Notes and Artifacts say what to pick instead.
function syncOverlays(){{const page=!!readerPage(),lib=KIND==='library',show=lib&&!page;empty.hidden=lib||page;showHome(show);document.body.classList.toggle('reader-blank',!page)}}
function shellUrl(k,src,folder){{const p=new URLSearchParams();if(k==='html')p.set('vault','html');if(src)p.set('src',src);if(folder)p.set('folder',folder);const q=p.toString();return (k==='library'?'/':'/vault')+(q?'?'+q:'')}}
function readerFolder(){{try{{return new URLSearchParams(reader.contentWindow.location.search).get('folder')||''}}catch(e){{return ''}}}}
// What follows the page in the reader, on its load and again whenever a tab brings another frame forward. The shell's
// other parts (find, the palette, the tab bar) hang theirs on with onReaderLoad; each binds named listeners, so running
// again binds nothing twice, and one that throws is reported without stopping the rest.
const READER_HOOKS=[]; function onReaderLoad(fn){{READER_HOOKS.push(fn)}}
function readerLoaded(){{readerFollow();for(const fn of READER_HOOKS)try{{fn()}}catch(e){{setTimeout(()=>{{throw e}})}}}}
if(native)onReaderLoad(()=>{{try{{const w=reader.contentWindow;w.addEventListener('mousedown',readerDragDown,true);w.addEventListener('mouseup',readerDragUp,true)}}catch(e){{}}}});
function readerFollow(){{try{{reader.contentWindow.addEventListener('keydown',sideKey)}}catch(e){{}}syncOverlays();outlineLoaded();const src=currentSrc();if(!src){{if(!readerPage()){{history.replaceState(null,'',shellUrl(KIND,''));document.title=VAULT.name}}return}}
const k=vaultOf(src);if(k&&KIND!=='library'&&viewOf(k)!==KIND&&traversed())switchVault(viewOf(k),true);highlight(src);history.replaceState(null,'',shellUrl(KIND,src,KIND==='library'&&!k?readerFolder():''));let t='';try{{t=reader.contentDocument.title}}catch(e){{}}document.title=(t||src.split('/').pop())+' — '+VAULT.name;rememberLast(src)}}
{tabs_js}
// The page a vault shows is its last page, the one a switch back brings up. The page the shell opened on can load before
// its tree does (the two race, a few ms apart), when `vaultOf` can't yet say whose it is: the trees' arrival asks again.
function rememberLast(src){{const k=vaultOf(src);if(k)store(keyOf(k)+'last',src);return k}}
// A saved document or conversation, into the reader: by its vault path when it lives in a vault (the tree highlights it,
// and a page in Artifacts keeps its link's context), else as it was read, with the folder it was read with.
function itemHref(item,action){{const src=item.source||item.document_source||'',p=new URLSearchParams();let base='/view';
if(src.startsWith('service://selection/')){{base='/quick';p.set('text',item.selection||'');if(item.folder)p.set('folder',item.folder)}}else{{p.set('src',item.vault_path||src);const folder=item.vault==='notes'?(rootOf('notes')||item.folder):item.vault==='html'?'':item.folder;if(folder)p.set('folder',folder)}}
if(action){{p.set('history',item.request_id);p.set('history_action',action)}}return base+'?'+p}}
function openItem(item,action,inTab){{const k=KIND!=='library'&&(item.vault?viewOf(item.vault):'')!==KIND?'library':KIND;if(inTab){{openTab(itemHref(item,action),{{kind:k}});return}}if(k!==KIND)switchVault(k,true);navigate(itemHref(item,action))}}
let filterTimer; filter.oninput=()=>{{clearTimeout(filterTimer);filterTimer=setTimeout(applyFilter,150)}};
async function applyFilter(){{const q=filter.value.trim(),k=KIND;if(q.length<2){{renderTree();return}}const lib=k==='library',kinds=shownIn(k).filter(rootOf);
try{{const found=await Promise.all(kinds.map(vk=>api('/api/vault/search?vault='+vk+'&q='+encodeURIComponent(q)+(lib?'&limit=25':'')).then(d=>d.items.map(i=>({{...i,k:vk}})))));if(k!==KIND)return;const items=found.flat();hidePeek();NODES.clear();items.forEach(i=>NODES.set(i.path,{{n:i,crumbs:(i.folder||'').split('/').filter(Boolean),k:i.k}}));
tree.innerHTML='<ul class="root results">'+items.map(i=>`<li><a class=file target=reader href="${{esc(viewHref(i.path,i.k))}}" data-path="${{esc(i.path)}}" data-vault=${{i.k}}><span class=lbl>${{esc(i.k==='html'?(i.title||i.name):label(i,i.k))}}</span><small>${{esc(lib||i.k!==k?GROUPS[i.k]+(i.folder?' › '+i.folder:''):(i.folder||'/'))}}</small></a></li>`).join('')+(items.length?'':`<li class=none>${{lib?'Nothing matches.':'No '+UNIT+'s match.'}}</li>`)+'</ul>';highlight(currentSrc())}}catch(e){{if(k===KIND)tree.innerHTML=`<div class=none>${{esc(e.message)}}</div>`}}}}
document.addEventListener('keydown',e=>{{const typing=/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement&&document.activeElement.tagName);if(e.key==='/'&&!typing&&!e.metaKey&&!e.ctrlKey&&!pageOnly()){{e.preventDefault();if(!pinned())sideOut(true);filter.focus();filter.select()}}else if(e.key==='Escape'&&document.activeElement===filter){{filter.value='';applyFilter();filter.blur()}}}});
// MARK: row menu — the app's rendered menu (static/app-menu.js). The server says what a row really is (its real file, and
// the link on the way); ⌥ turns each Reveal into a Copy. A mousedown while the answer is in flight means it came too late.
let menuSeq=0; document.addEventListener('mousedown',()=>{{menuSeq++}},true);
side.addEventListener('contextmenu',async e=>{{const inTree=tree.contains(e.target);
// Blank sidebar chrome and gaps between rows belong to the tree menu too. Leave the switch, fields and buttons alone.
if(!inTree&&e.target.closest('button, a, input, select, .vault-switch, .add-panel, #side-grip'))return;
const row=e.target.closest('#tree .file, #tree summary:not(.group-head)');if(!window.OnyxMenu)return;
// The tree's background (including Library's headings): reveal the page being read, fold every folder, and, in
// Artifacts, make a new folder at the top level. The word follows the page's own vault, so an artifact is a Page.
if(!row){{e.preventDefault();const tgt=revealTarget(),rk=tgt?vaultOf(tgt):(HTML?'html':'notes');
const items=[{{id:'reveal-current',label:'Reveal Current '+(rk==='html'?'Page':'Note'),enabled:!!tgt}},{{id:'collapse-all',label:'Collapse All',enabled:!collapsed()}}];
if(HTML&&rootOf('html'))items.push({{separator:true}},{{id:'new-folder',label:'New Folder'}});
OnyxMenu.open({{items,x:e.clientX,y:e.clientY,label:VAULT.name+' actions',onSelect:id=>{{if(id==='reveal-current')revealCurrent();else if(id==='collapse-all')collapseAll();else if(id==='new-folder')newFolder('')}}}});return}}
e.preventDefault();
// WebKit on macOS selects the word under a right-click before this event fires (for Look Up); a row isn't text to select.
const sel=getSelection();if(sel&&sel.anchorNode&&row.contains(sel.anchorNode))sel.removeAllRanges();const holder=row.closest('[data-vault]'),vk=holder&&holder.dataset.vault,path=row.dataset.path||row.parentElement.dataset.path;if(!path||!vk)return;
let x=e.clientX,y=e.clientY;if(!x&&!y){{const r=row.getBoundingClientRect();x=r.left+16;y=r.bottom}}
const seq=++menuSeq;let d;try{{d=await api('/api/vault/entry?vault='+vk+'&path='+encodeURIComponent(path))}}catch(err){{OnyxMenu.toast(err.message,'bad');return}}if(seq!==menuSeq)return;
const items=[];if(!d.is_dir)items.push({{id:'open',label:'Open',enabled:d.exists}},{{id:'open-tab',label:'Open in New Tab',enabled:d.exists}});
items.push({{id:'reveal',label:'Reveal in Finder',enabled:!!d.real,alt:{{id:'copy',label:'Copy Path'}}}});
if(d.link)items.push({{id:'reveal-link',label:'Reveal Link in Finder',alt:{{id:'copy-link',label:'Copy Link Path'}}}});
// Artifacts' own rows (see grip): a folder of its own takes a new one, and what the vault owns renames (folders — a page
// is labelled by its title, not its name), pins, and comes out.
if(vk==='html'){{const own=row.tagName==='SUMMARY'&&row.parentElement.dataset.rel!==undefined,entry=row.dataset.entry,pinned=row.dataset.pinned!==undefined,more=[];
if(own)more.push({{id:'new-folder',label:'New Folder'}});if(entry&&row.tagName==='SUMMARY')more.push({{id:'rename',label:'Rename'}});
if(entry)more.push({{id:pinned?'unpin':'pin',label:pinned?'Unpin':'Pin to Top'}},{{id:'remove',label:'Remove from Artifacts'}});if(more.length)items.push({{separator:true}},...more)}}
OnyxMenu.open({{items,x,y,label:(d.is_dir?'Folder':vk==='html'?'Page':'Note')+' actions',returnFocus:row,onClose:()=>row.classList.remove('menu-for'),onSelect:id=>rowAction(id,d,row,vk)}});row.classList.add('menu-for')}});
function copyPath(p){{if(!navigator.clipboard)throw new Error('The clipboard is not available here.');return navigator.clipboard.writeText(p).then(()=>OnyxMenu.toast('Copied '+shortPath(p)))}}
async function rowAction(id,d,row,vk){{try{{if(id==='open'){{if(row.tagName==='A')row.click();else navigate(viewHref(d.path,vk))}}else if(id==='open-tab')openTab(viewHref(d.path,vk));else if(id==='copy')await copyPath(d.real);else if(id==='copy-link')await copyPath(d.path);else if(id==='reveal'||id==='reveal-link')await postJSON('/api/vault/reveal',{{vault:vk,path:d.path,which:id==='reveal'?'real':'link'}})
else if(id==='new-folder')newFolder(row.parentElement.dataset.rel);else if(id==='rename')renameRow(row);
else if(id==='pin'||id==='unpin'){{await postJSON('/api/vault/html/pin',{{path:row.dataset.entry,pinned:id==='pin'}});await loadTree()}}
else if(id==='remove'){{const r=await postJSON('/api/vault/html/remove',{{path:row.dataset.entry}});await loadTree();OnyxMenu.toast(r.removed==='link'?'Removed the link · the original is untouched':'Removed the folder')}}}}catch(err){{OnyxMenu.toast(err.message||String(err),'bad')}}}}
// MARK: add panel (Artifacts only; CSS keeps it out of the other views)
const DEST_KEY=keyOf('html')+'dest';
function destinations(){{const out=[{{rel:'',label:'Top level'}}],d=TREES.html;(function walk(n,depth){{for(const c of n.children||[]){{if(c.kind==='dir'&&!c.linked){{out.push({{rel:c.rel,label:'\\u00a0'.repeat(depth*3)+c.name}});walk(c,depth+1)}}}}}})(d&&d.tree||{{children:[]}},0);return out}}
function fillDestinations(){{const sel=$('#add-dest');if(!sel)return;const keep=sel.value||recall(DEST_KEY)||'';const opts=destinations();sel.innerHTML=opts.map(o=>`<option value="${{esc(o.rel)}}">${{esc(o.label)}}</option>`).join('');sel.value=opts.some(o=>o.rel===keep)?keep:''}}
function addStatus(text,tone){{const el=$('#add-status');if(!el)return;el.textContent=text;el.className='field-help'+(tone?' '+tone:'')}}
async function postJSON(url,body){{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:TOKEN,...body}})}});const d=await r.json().catch(()=>({{ok:false,error:'HTTP '+r.status}}));if(!r.ok||d.ok===false)throw new Error(d.error||'HTTP '+r.status);return d}}
function shortPath(p){{return String(p).replace(/^\\/Users\\/[^/]+/,'~')}}
async function linkTargets(targets){{targets=(targets||[]).filter(Boolean);if(!targets.length)return;addStatus('Linking…');try{{const d=await postJSON('/api/vault/html/link',{{parent:$('#add-dest').value,targets}});let msg='Linked '+d.linked.length+(d.linked.length===1?' item':' items');if(d.context_roots.length)msg+=' · answers can now cite '+d.context_roots.map(shortPath).join(', ');if(d.errors.length)msg+=' · skipped: '+d.errors.join('; ');addStatus(msg,d.errors.length?'bad':'ok');$('#add-path').value='';await loadTree()}}catch(e){{addStatus(e.message,'bad')}}}}
{{const toggle=$('#add-toggle'),panel=$('#add-panel');toggle.onclick=()=>{{panel.hidden=!panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)fillDestinations()}};
$('#add-dest').onchange=e=>store(DEST_KEY,e.target.value);
$('#add-pick-files').onclick=async()=>{{try{{const picked=await window.webkit.messageHandlers.askwPick.postMessage({{kind:'html',initial:''}});await linkTargets(Array.isArray(picked)?picked:[picked])}}catch(e){{addStatus(e.message,'bad')}}}};
$('#add-pick-folder').onclick=async()=>{{try{{const picked=await window.webkit.messageHandlers.askwPick.postMessage({{kind:'folder',initial:'',prompt:'Link Folder',message:'Choose a folder of HTML pages to link into Artifacts'}});await linkTargets([picked])}}catch(e){{addStatus(e.message,'bad')}}}};
$('#add-path-go').onclick=()=>linkTargets([$('#add-path').value.trim()]);$('#add-path').onkeydown=e=>{{if(e.key==='Enter'){{e.preventDefault();linkTargets([e.target.value.trim()])}}}};
async function makeFolder(){{const name=$('#add-folder-name').value.trim();if(!name)return;try{{const d=await postJSON('/api/vault/html/folder',{{parent:$('#add-dest').value,name}});$('#add-folder-name').value='';store(DEST_KEY,d.rel);addStatus('Created '+d.rel,'ok');await loadTree()}}catch(e){{addStatus(e.message,'bad')}}}}
$('#add-mkdir').onclick=makeFolder;$('#add-folder-name').onkeydown=e=>{{if(e.key==='Enter'){{e.preventDefault();makeFolder()}}}}}}
// MARK: reorganising (Artifacts) — drag a row the vault owns onto one of its folders, or onto the list's empty space for
// the top level; a row inside a folder counts as that folder, as in Finder's list view, and a shut folder held under the
// pointer springs open. The server moves the link itself, never what it points at (vault.move_entry).
let DRAG=null,dropMark=null,springTimer=0;
function parentRel(rel){{const i=rel.lastIndexOf('/');return i<0?'':rel.slice(0,i)}}
function dropAt(el){{if(!DRAG||!el||!el.closest)return null;let rel,mark;const d=el.closest('#tree details[data-rel]'),g=el.closest('#tree li.group');
if(d){{rel=d.dataset.rel;mark=d.querySelector(':scope > summary')}}else if(g){{const gd=g.querySelector(':scope > details');if(gd.dataset.group!=='html')return null;rel='';mark=gd.querySelector(':scope > summary')}}else if(KIND==='html'&&tree.contains(el)){{rel='';mark=tree}}else return null;
if(rel===DRAG.parent||(DRAG.dir&&(rel===DRAG.rel||rel.startsWith(DRAG.rel+'/'))))return null;return {{rel,mark}}}}
function markDrop(t){{const m=t?t.mark:null;if(m===dropMark)return;if(dropMark)dropMark.classList.remove(dropMark===tree?'drop-root':'drop-into');if(m)m.classList.add(m===tree?'drop-root':'drop-into');dropMark=m;clearTimeout(springTimer);
const d=m&&m.tagName==='SUMMARY'?m.parentElement:null;if(d&&!d.open)springTimer=setTimeout(()=>{{if(dropMark===m)d.open=true}},650)}}
function endDrag(){{DRAG=null;markDrop(null);tree.querySelectorAll('.dragging').forEach(r=>r.classList.remove('dragging'))}}
tree.addEventListener('dragstart',e=>{{const row=e.target.closest&&e.target.closest('#tree [data-entry]'),root=rootOf('html');if(!row||!root)return;const entry=row.dataset.entry,rel=entry.slice(root.length+1);
DRAG={{entry,rel,parent:parentRel(rel),dir:row.tagName==='SUMMARY'}};e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('application/x-onyx-entry',entry);row.classList.add('dragging');hidePeek()}});
tree.addEventListener('dragover',e=>{{if(!DRAG)return;const t=dropAt(e.target);markDrop(t);if(t){{e.preventDefault();e.dataTransfer.dropEffect='move'}}}});
document.addEventListener('dragover',e=>{{if(DRAG&&!tree.contains(e.target))markDrop(null)}});
tree.addEventListener('drop',e=>{{const t=DRAG&&dropAt(e.target),entry=DRAG&&DRAG.entry;endDrag();if(!t)return;e.preventDefault();moveEntry(entry,t.rel)}});
tree.addEventListener('dragend',endDrag);
async function moveEntry(entry,dest){{try{{const d=await postJSON('/api/vault/html/move',{{path:entry,dest}});remap(d.from,d.path);if(dest)setOpen('html',rootOf('html')+'/'+dest,true);await loadTree();OnyxMenu.toast('Moved to '+(dest?dest.split('/').join(' › '):'the top level'))}}catch(err){{OnyxMenu.toast(err.message||String(err),'bad')}}}}
// A move or rename changes the vault path of everything beneath it, so what the shell remembers by path follows: the
// folders left shut, the page each view comes back to, the + panel's destination, the tabs behind (remapTabs), and the
// page open now (loaded again from its new path, which is how it keeps its link's context).
function remap(from,to){{if(!from||from===to)return;const move=p=>p===from?to:p&&p.startsWith(from+'/')?to+p.slice(from.length):'';
const f=FOLDS.html;for(const p of [...f]){{const q=move(p);if(q){{f.delete(p);f.add(q)}}}}store(foldKey('html'),JSON.stringify([...f]));
const lk=keyOf('html')+'last',last=move(recall(lk));if(last)store(lk,last);
const root=rootOf('html'),dest=recall(DEST_KEY),moved=dest&&move(root+'/'+dest);if(moved)store(DEST_KEY,moved.slice(root.length+1));
remapTabs(move);const src=move(currentSrc());if(src){{let hash='';try{{hash=reader.contentWindow.location.hash}}catch(e){{}}navigate(viewHref(src,'html')+hash)}}}}
// New Folder and Rename name a row in place, as Finder does: Return keeps the name, Escape (or nothing typed) puts it back.
function nameInPlace(slot,initial,save){{const input=document.createElement('input'),was=[...slot.childNodes],held=slot.closest('[draggable]');
input.className='name-edit';input.value=initial;input.spellcheck=false;input.autocomplete='off';input.setAttribute('aria-label',initial?'New name':'Folder name');if(held)held.draggable=false;slot.replaceChildren(input);let done=false;
async function finish(keep){{if(done)return;done=true;const name=input.value.trim();if(keep&&name&&name!==initial){{try{{await save(name);return}}catch(err){{OnyxMenu.toast(err.message||String(err),'bad')}}}}
if(slot.dataset.temp!==undefined)slot.closest('li').remove();else{{slot.replaceChildren(...was);if(held)held.draggable=true}}}}
input.addEventListener('keydown',e=>{{e.stopPropagation();if(e.key==='Enter'){{e.preventDefault();finish(true)}}else if(e.key==='Escape'){{e.preventDefault();finish(false)}}}});
// Inside a folder's summary a click, or Space, would also open or shut the folder.
input.addEventListener('click',e=>e.preventDefault());input.addEventListener('keyup',e=>{{if(e.key===' ')e.preventDefault()}});
input.addEventListener('blur',()=>finish(true));input.focus();input.select()}}
function renameRow(row){{const lbl=row.querySelector('.lbl');if(lbl&&row.dataset.entry)nameInPlace(lbl,lbl.textContent,async name=>{{const d=await postJSON('/api/vault/html/rename',{{path:row.dataset.entry,name}});remap(d.from,d.path);await loadTree()}})}}
function newFolder(rel){{let list;if(rel){{const d=tree.querySelector(`details[data-rel="${{CSS.escape(rel)}}"]`);if(!d)return;d.open=true;list=d.querySelector(':scope > ul')}}else if(KIND==='library'){{const g=tree.querySelector('details[data-group=html]');if(!g)return;g.open=true;list=g.querySelector(':scope > ul')}}else{{list=tree.querySelector(':scope > ul.root');if(!list){{tree.innerHTML='<ul class=root></ul>';list=tree.firstChild}}}}
const li=document.createElement('li');li.innerHTML=`<span class="file new-folder">${{ICON.folder}}<span class=lbl data-temp></span></span>`;list.prepend(li);li.scrollIntoView({{block:'nearest'}});
nameInPlace(li.querySelector('.lbl'),'',async name=>{{await postJSON('/api/vault/html/folder',{{parent:rel,name}});await loadTree()}})}}
// MARK: Obsidian look — while the vault's file-explorer look is in force (body.obsidian-tree, CSS from /api/sidebar-theme),
// each top-level folder takes its own Obsidian colour: by name in Notes, else by position — Artifacts always by position,
// since its folder names never match the vault's. Kept live, like the reader's Markdown styles.
let SIDE_THEME={sidebar_state};
function applyTints(){{const on=document.body.classList.contains('obsidian-tree'),list=SIDE_THEME.folders||[],byName=new Map(list.map(f=>[f.name.toLowerCase(),f]));
const grouped=k=>[k,tree.querySelectorAll(`:scope > ul.root > li.group > details[data-group=${{k}}] > ul > li > details`)];
const tops=KIND==='library'?BOTH.map(grouped):[[KIND,tree.querySelectorAll(':scope > ul.root > li:not(.group) > details')]].concat(KIND==='notes'?EXTRA.map(grouped):[]);
for(const [k,rows] of tops)rows.forEach((d,i)=>{{const li=d.parentElement,name=(d.querySelector(':scope > summary .lbl')||{{}}).textContent||'';
const f=on&&list.length?((k==='notes'&&byName.get(name.toLowerCase()))||list[i%list.length]):null;
for(const [prop,value] of [['--folder-color',f&&f.color],['--guide-color',f&&(f.guide||f.color)],['--folder-hover',f&&f.hover]]){{if(value)li.style.setProperty(prop,value);else li.style.removeProperty(prop)}}}})}}
// MARK: vault look — the whole app in the vault's colours (vault_look) while Match vault appearance is on: its tokens on
// :root.vault-look, and the glass and the native window in the vault's mode and ground. Kept live with the sidebar's look;
// `force` is Settings turning it on or off, when the revision last drawn may be the one there is now.
let LOOK={look_state};
function syncAppearance(){{const h=native&&window.webkit.messageHandlers.askwAppearance;if(h)Promise.resolve(h.postMessage({{theme:LOOK.css?LOOK.mode:(document.documentElement.dataset.theme||'system')}})).catch(()=>{{}})}}
function applyLook(d){{const changed=LOOK.revision!==d.revision||LOOK.page_enabled!==d.page_enabled;LOOK=d;if(changed){{PAGE_LOOKS.clear();savePageLooks();PAGE_LOOK=null}}paintLook();syncAppearance();for(const t of TABS.list)try{{if(t.frame&&t.frame.contentWindow.askwRefreshLook)t.frame.contentWindow.askwRefreshLook(d)}}catch(e){{}}try{{window.onyxPageLook(reader.contentWindow,reader.contentWindow.askwPageLook)}}catch(e){{}}}}
async function syncSidebarTheme(force){{if(document.hidden&&!force)return;try{{const [d,l]=await Promise.all([api('/api/sidebar-theme'),api('/api/vault-look')]);const changed=force||d.revision!==SIDE_THEME.revision;SIDE_THEME=d;$('#sidebar-theme').textContent=d.css||'';if(force||l.revision!==LOOK.revision||l.page_enabled!==LOOK.page_enabled)applyLook(l);else if(changed)paintLook()}}catch(e){{}}}}
setInterval(syncSidebarTheme,3000); document.addEventListener('visibilitychange',()=>syncSidebarTheme());
// The visible reader owns the page palette. A background frame may finish loading
// or change its theme, but cannot recolour the window until its tab is selected.
// Keep 32 looks for revisits within this window; theme changes use different cache
// keys. The reader always measures again, so a changed artifact replaces its look.
let PAGE_LOOK=null;
const PAGE_LOOKS=new Map();
try{{for(const [k,v] of JSON.parse(sessionStorage.getItem('askw:page-looks')||'[]').slice(-32))if(typeof k==='string'&&v&&typeof v.css==='string')PAGE_LOOKS.set(k,v)}}catch(e){{}}
function savePageLooks(){{try{{sessionStorage.setItem('askw:page-looks',JSON.stringify([...PAGE_LOOKS]))}}catch(e){{}}}}
function pageLookKey(href){{try{{const u=new URL(href,location.origin);return u.origin===location.origin&&u.pathname==='/view'?JSON.stringify([u.searchParams.get('src'),LOOK.revision,document.documentElement.dataset.theme,matchMedia('(prefers-color-scheme:dark)').matches]):null}}catch(e){{return null}}}}
window.onyxCachedPageLook=href=>LOOK.page_enabled!==false?PAGE_LOOKS.get(pageLookKey(href))||null:null;
function paintLook(){{const d=PAGE_LOOK||LOOK;$('#vault-look').textContent=(LOOK.css||'')+(PAGE_LOOK?'\\n'+PAGE_LOOK.css:'');document.documentElement.classList.toggle('vault-look',!!d.css);
document.documentElement.classList.toggle('page-look',!!PAGE_LOOK);document.body.classList.toggle('obsidian-tree',!PAGE_LOOK&&!!SIDE_THEME.css);
setGlassVault(d.css?d:null);applyTints();document.dispatchEvent(new Event('onyx:look'))}}
function usePageLook(d){{const next=LOOK.page_enabled!==false&&d&&d.css?d:null;if((PAGE_LOOK&&PAGE_LOOK.revision)===(next&&next.revision))return;PAGE_LOOK=next;paintLook()}}
window.onyxPageLook=(w,d)=>{{if(w!==reader.contentWindow)return;const key=pageLookKey(w.location.href);if(key){{PAGE_LOOKS.delete(key);if(d&&d.css)PAGE_LOOKS.set(key,d);if(PAGE_LOOKS.size>32)PAGE_LOOKS.delete(PAGE_LOOKS.keys().next().value);savePageLooks()}}usePageLook(d)}};
function previewPageLook(href){{usePageLook(window.onyxCachedPageLook(href))}}
onReaderLoad(()=>{{try{{usePageLook(reader.contentWindow.askwPageLook)}}catch(e){{usePageLook(null)}}}});
document.addEventListener('click',e=>{{if(e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;const a=e.target.closest&&e.target.closest('a[target]');if(a&&/^reader(-|$)/.test(a.target))previewPageLook(a.href)}});
// MARK: hover card — a page's whole title, its one line, where it lives, what it is. The first hover waits a beat; after
// that it follows the pointer row to row at once. A click, scroll, right-click menu, Escape, or leaving puts it away.
const peek=$('#peek'); let peekRow=null, peekTimer=0, peekWarmUntil=0;
function updated(ts){{const a=ago(ts);return !a?'':/\\d[mhdw]$/.test(a)?'Updated '+a+' ago':'Updated '+a}}
function kindOf(n,k){{if(k==='html')return 'HTML page';const x=(n.ext||(/\\.[^.]+$/.exec(n.name||'')||[''])[0]).replace('.','').toUpperCase();return /^(MD|MARKDOWN)$/.test(x)?'Markdown note':x?x+' file':'Note'}}
function showPeek(row){{const e=NODES.get(row.dataset.path);if(!e||(window.OnyxMenu&&OnyxMenu.isOpen()))return;const n=e.n;
if(peekRow&&peekRow!==row)peekRow.removeAttribute('aria-describedby');peekRow=row;row.setAttribute('aria-describedby','peek');
const where=(KIND==='library'||e.k!==KIND?[GROUPS[e.k]]:[]).concat(e.crumbs).join(' › ')||VAULTS[viewOf(e.k)].name,what=n.missing?'Link target is missing: '+shortPath(n.target||n.name):[kindOf(n,e.k),updated(n.mtime)].filter(Boolean).join(' · ');
peek.innerHTML=`<p class=peek-title>${{esc(label(n,e.k))}}</p>${{n.summary?`<p class=peek-sum>${{esc(n.summary)}}</p>`:''}}<p class=peek-row>${{ICON.folder}}<span>${{esc(where)}}</span></p><p class=peek-row>${{n.missing?ICON.link:ICON.doc}}<span>${{esc(what)}}</span></p>`;
peek.hidden=false;const r=row.getBoundingClientRect(),side=$('#vault-side').getBoundingClientRect(),w=peek.offsetWidth,h=peek.offsetHeight,beside=side.right+10+w<=innerWidth-8;
peek.style.left=(beside?side.right+10:Math.max(8,Math.min(r.left,innerWidth-w-8)))+'px';peek.style.top=Math.max(8,Math.min(beside?r.top-4:r.bottom+6,innerHeight-h-8))+'px';requestAnimationFrame(()=>peek.classList.add('show'))}}
function hidePeek(){{clearTimeout(peekTimer);if(peekRow){{peekRow.removeAttribute('aria-describedby');peekRow=null}}if(!peek.hidden){{peekWarmUntil=Date.now()+350;peek.classList.remove('show');peek.hidden=true}}}}
function wantPeek(row,delay){{clearTimeout(peekTimer);if(row===peekRow)return;peekTimer=setTimeout(()=>showPeek(row),!peek.hidden||Date.now()<peekWarmUntil?0:delay)}}
tree.addEventListener('mouseover',e=>{{const row=e.target.closest('#tree .file');if(row)wantPeek(row,450)}});
tree.addEventListener('mouseout',e=>{{const row=e.target.closest('#tree .file');if(row&&!row.contains(e.relatedTarget)){{clearTimeout(peekTimer);peekTimer=setTimeout(hidePeek,90)}}}});
tree.addEventListener('focusin',e=>{{const row=e.target.closest('#tree .file');if(row&&row.matches(':focus-visible'))wantPeek(row,200)}});
for(const ev of ['focusout','scroll','click','contextmenu'])tree.addEventListener(ev,hidePeek,{{passive:true}}); document.addEventListener('keydown',e=>{{if(e.key==='Escape')hidePeek()}}); window.addEventListener('blur',hidePeek);
{panels_js}
{search_js}
{find_js}
// MARK: home — Library's page while nothing is open: open a file or URL, what you had open lately (whatever it was), and
// your latest asks. Fetched fresh each time it shows; a card is a plain link into the reader, an ask opens its conversation.
function docTag(d){{return d.vault==='notes'?'Note':d.vault==='html'?'Artifact':({{pdf:'PDF',markdown:'Markdown',text:'Text',html:'HTML','remote-html':'Web'}})[d.kind]||'File'}}
function docWhere(d){{if(d.vault)return [GROUPS[d.vault]].concat(d.vault_folder?d.vault_folder.split('/'):[]).join(' › ');if(/^https?:/i.test(d.source))try{{return new URL(d.source).host}}catch(e){{}}return shortPath(d.source.replace(/\\/[^/]*$/,''))}}
function docCard(d){{return `<a class=home-card target=reader href="${{esc(itemHref(d,''))}}" data-source="${{esc(d.source)}}" title="${{esc(d.vault_path||d.source)}}"><span class=t>${{esc(d.title)}}</span><span class=m><span class="tag ${{esc(d.vault||'')}}">${{esc(docTag(d))}}</span><span class=w>${{esc(docWhere(d))}}</span><span class=a>${{esc(ago(d.last_opened_at))}}</span></span></a>`}}
// A follow-up is saved as an ask of its own naming the one it followed (so is Ask again): list each conversation once, by
// its latest turn, the one Continue picks up from, with how many asks it holds.
function threads(items){{const byId=new Map(items.map(c=>[c.request_id,c])),followed=new Set(items.map(c=>c.parent_request_id));
return items.filter(c=>!followed.has(c.request_id)).map(c=>{{let n=1;for(let p=byId.get(c.parent_request_id);p&&n<items.length;p=byId.get(p.parent_request_id))n++;return {{...c,turns:n}}}})}}
function askRow(c){{return `<button type=button class=home-ask data-id="${{esc(c.request_id)}}"><span class=t><strong>${{esc(c.document_title||'Untitled')}}</strong><span class=a>${{c.turns>1?c.turns+' asks · ':''}}${{esc(ago(c.started_at))}}</span></span><span class=q>${{esc(c.question||({{ask:'Question',eli5:'ELI5',prove:'Prove it'}})[c.action]||c.action)}}</span></button>`}}
async function loadHome(){{try{{const d=await api('/api/library'),docs=(d.documents||[]).slice(0,9),asks=threads(d.conversations||[]).filter(c=>!c.recent_hidden).slice(0,6);PANELS.remember(d.conversations);
$('#home-docs').innerHTML=docs.length?docs.map(docCard).join(''):'<div class=home-empty>Documents you open will appear here.</div>';
$('#home-asks').innerHTML=asks.length?asks.map(askRow).join(''):'<div class=home-empty>Your completed answers will be saved here.</div>'}}catch(e){{$('#home-docs').innerHTML=`<div class=home-empty>${{esc(e.message)}}</div>`}}}}
$('#home-asks').addEventListener('click',e=>{{const b=e.target.closest('[data-id]');if(b)PANELS.showConversation(b.dataset.id)}}); $('#home-all').onclick=()=>PANELS.openHistory();
// A card's menu, as the sidebar's rows have one. Remove from Recents hides the page or the thread from these lists (and the
// palette's, the Dock's and the phone's); it deletes nothing, and opening the page again or asking on brings it back.
home.addEventListener('contextmenu',e=>{{const card=e.target.closest('#home-docs .home-card, #home-asks .home-ask');if(!card||!window.OnyxMenu)return;e.preventDefault();
const sel=getSelection();if(sel&&sel.anchorNode&&card.contains(sel.anchorNode))sel.removeAllRanges();
let x=e.clientX,y=e.clientY;if(!x&&!y){{const r=card.getBoundingClientRect();x=r.left+16;y=r.bottom}}const ask=card.classList.contains('home-ask');
const items=ask?[{{id:'open',label:'Open Conversation'}}]:[{{id:'open',label:'Open'}},{{id:'open-tab',label:'Open in New Tab'}}];items.push({{separator:true}},{{id:'remove',label:'Remove from Recents'}});
OnyxMenu.open({{items,x,y,label:(ask?'Ask':'Page')+' actions',returnFocus:card,onClose:()=>card.classList.remove('menu-for'),onSelect:id=>homeAction(id,card,ask)}});card.classList.add('menu-for')}});
async function homeAction(id,card,ask){{try{{if(id==='open')card.click();else if(id==='open-tab')openTab(card.getAttribute('href'));
else if(id==='remove'){{await postJSON('/api/recent/remove',ask?{{request_id:card.dataset.id}}:{{source:card.dataset.source}});card.remove();await loadHome()}}}}catch(err){{OnyxMenu.toast(err.message||String(err),'bad')}}}}
$('#open-form').onsubmit=e=>{{e.preventDefault();let s=$('#open-src').value.trim(),hash='';if(!s)return;const m=s.match(new RegExp('^((?:file://|/|~).*[.](?:html?|md|markdown|txt|pdf))(#[^/]*)$','i'));if(m){{s=m[1];hash=m[2]}}const vk=vaultOf(s),f=$('#open-folder').value.trim();navigate((vk?viewHref(s,vk):'/view?src='+encodeURIComponent(s)+(f?'&folder='+encodeURIComponent(f):''))+hash);$('#open-src').value=''}};
$('#open-folder').oninput=e=>{{$('#open-folder-label').textContent=shortPath(e.target.value.trim())||'the default folder'}};
home.querySelectorAll('[data-pick]').forEach(b=>b.onclick=async()=>{{const el=$('#'+b.dataset.target);try{{const p=await window.webkit.messageHandlers.askwPick.postMessage({{kind:b.dataset.pick,initial:el.value}});if(p){{el.value=p;el.dispatchEvent(new Event('input'))}}}}catch(e){{OnyxMenu.toast(e.message,'bad')}}}});
// Library shown again while already there goes home: the reader empties and the home page comes back.
function goHome(){{if(readerPage())reader.src='about:blank';highlight('');empty.hidden=true;showHome(true);history.replaceState(null,'','/');document.title=VAULT.name}}
$('#open-settings').onclick=()=>PANELS.openSettings(); $('#open-history').onclick=()=>PANELS.openHistory();
tree.addEventListener('click',e=>{{const a=e.target.closest('[data-settings]');if(a){{e.preventDefault();PANELS.openSettings(a.dataset.settings)}}}});
// MARK: switch — Library ⇄ Notes ⇄ Artifacts in place. The pill slides and the width glides (CSS, off the body's kind
// class), the list swaps from TREES with a quick fade and is fetched again behind it, and the reader brings back that
// vault's last page, or, for Library, the home page. `follow` means the reader has already moved (history, or a saved
// conversation opening): then only the sidebar does. A modified click, or a page without script, still loads the link.
const switchLinks=[...document.querySelectorAll('.vault-switch a')],treeScroll={{}};
function switchVault(k,follow){{if(!VAULTS[k])return;if(k===KIND){{if(k==='library'&&!follow)goHome();return}}
if(window.OnyxMenu)OnyxMenu.close();menuSeq++;hidePeek();clearTimeout(filterTimer);filter.value='';treeScroll[KIND]=tree.scrollTop;
setKind(k);document.body.classList.remove('kind-library','kind-notes','kind-html');document.body.classList.add('kind-'+k);
for(const a of switchLinks){{const on=a.dataset.kind===k;a.classList.toggle('active',on);if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')}}
$('.brand-name').textContent=VAULT.name;filter.placeholder='Filter '+VAULT.units+'… (press /)';filter.setAttribute('aria-label','Filter '+VAULT.units);tree.setAttribute('aria-label',VAULT.tree);$('#empty-hint').textContent=VAULT.empty;
$('#add-panel').hidden=true;$('#add-toggle').setAttribute('aria-expanded','false');
const cached=shownIn(k).every(x=>TREES[x]);if(cached){{showTree();tree.scrollTop=treeScroll[k]||0;highlight(currentSrc())}}else tree.innerHTML='<div class=none>Loading…</div>';
document.body.classList.add('kind-still');requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('kind-still')));
const fresh=loadTree();if(follow){{syncOverlays();return}}if(k==='library'){{goHome();return}}
home.hidden=true;history.replaceState(null,'',shellUrl(k,''));document.title=VAULT.name;
(cached?Promise.resolve():fresh).then(()=>{{if(k!==KIND)return;const last=rootOf(k)?recall(KEY+'last'):null;empty.hidden=!!last;if(last)navigate(viewHref(last,k));else if(readerPage())reader.src='about:blank'}})}}
for(const a of switchLinks)a.addEventListener('click',e=>{{if(e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();switchVault(a.dataset.kind)}});
// The app menu comes through these: File ▸ Library / Vault / Artifacts switch in place (and load the page when it isn't
// this one), Settings… (⌘,) and Recent Conversations (⌘Y) open their dialogs, Search… (⌘P) the palette, Edit ▸
// Find's items (⌘F, ⌘G, ⇧⌘G) the find bar, and the tab items and outside opens the tabs (TAB_SHELL, tabs_ui.py).
window.onyxVault={{switchTo:k=>{{if(!VAULTS[k])return false;switchVault(k);return true}}}};
// ⌘E: the page showing turns into its editor and back (ask.js, toggleEdit), from View ▸ Toggle Editing in the app or from
// the key typed out here in the sidebar; typed inside the page, ask.js takes it itself. A page that can't be edited says so.
function readerEdit(){{try{{const w=reader.contentWindow;if(readerPage()&&w&&w.askwToggleEdit){{w.askwToggleEdit();return true}}}}catch(e){{}}return false}}
document.addEventListener('keydown',e=>{{if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&e.key.toLowerCase()==='e'&&readerEdit())e.preventDefault()}});
// MARK: page only — ⌘B leaves the page alone in the window, for an artifact whose own controls sit where Onyx's come out:
// a nav bar across the top of the page brought the tab bar's card out over its links, so they could not be clicked
// (2026-09-27). The panels go, pinned or not, with everything that brings one out (the edges, the tab bar's zone, the
// round toggle); the title band stops dragging the window; the widget hides its buttons in every page (ask.js asks
// pageOnly() as a page starts, and the frames already open are marked here); and the app hides the traffic lights
// (askwChrome). The pins are left alone, so ⌘B again puts back what was there. Not remembered: a relaunch into a
// window with no sidebar would be a puzzle. ⌘B in the editor is still bold: CodeMirror takes the key first, and a key a
// page has taken, or typed into rich text, is left to it. View ▸ Page Only in the app serves a click.
function pageOnly(){{return document.body.classList.contains('page-only')}}
function setPageOnly(on){{if(on===pageOnly())return true;const a=document.activeElement,lost=on&&!!a&&[side,outSide,tabBar].some(p=>p.contains(a));
document.body.classList.add('page-still');document.body.classList.toggle('page-only',on);
clearTimeout(sideTimer);clearTimeout(outTimer);clearTimeout(tabsTimer);outAtEdge=false;tabsAtEdge=false;if(on){{sideOut(false);outOut(false);tabsOut(false)}}
for(const f of stage.querySelectorAll('iframe'))try{{f.contentDocument.documentElement.toggleAttribute('data-askw-page-only',on)}}catch(e){{}}
if(lost)try{{reader.contentWindow.focus()}}catch(e){{}}
const h=native&&window.webkit.messageHandlers.askwChrome;if(h)Promise.resolve(h.postMessage({{pageOnly:on}})).catch(()=>{{}});
requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('page-still')));return true}}
function pageOnlyKey(e){{if(e.defaultPrevented||!(e.metaKey||e.ctrlKey)||e.altKey||e.shiftKey||(e.key||'').toLowerCase()!=='b'||(e.target&&e.target.isContentEditable))return;e.preventDefault();setPageOnly(!pageOnly())}}
document.addEventListener('keydown',pageOnlyKey);onReaderLoad(()=>{{try{{reader.contentWindow.addEventListener('keydown',pageOnlyKey)}}catch(e){{}}}});
window.onyxShell={{pageOnly:()=>pageOnly(),togglePageOnly:()=>setPageOnly(!pageOnly()),openSettings:section=>PANELS.openSettings(section),openHistory:()=>PANELS.openHistory(),openSearch:()=>SEARCH.open(),find:verb=>FIND.run(verb),edit:()=>readerEdit(),...TAB_SHELL}};
// Put back as it was left without a slide: the page opens with the sidebar already away.
if(/^(unpinned|collapsed)$/.test(recall(SIDE_KEY)||'')){{document.body.classList.add('side-still');setPinned(false);requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('side-still')))}}
// The outline likewise: docked at once if it was pinned, else away until its toggle is hovered.
document.body.classList.add('outline-still');setPane(relOn);applyOutlinePin();requestAnimationFrame(()=>requestAnimationFrame(()=>document.body.classList.remove('outline-still')));
// The tabs open last time come back (tabs_ui.py); a relaunch on Library reads the one that showed, in its view.
const RESTORED=restoreTabs();
// Both trees load up front: Library draws them together, and the first switch is as instant as the rest.
const FIRST=KIND; if(FIRST==='library'&&!INITIAL_SRC&&!RESTORED)loadHome();
(FIRST==='library'?loadTree():fetchTree(FIRST).then(()=>{{if(KIND===FIRST)showTree()}})).then(()=>{{for(const k of BOTH)if(!TREES[k])fetchTree(k).then(()=>{{if(KIND!=='library'&&shownIn(KIND).includes(k))showTree()}});{{const s=currentSrc();if(s){{rememberLast(s);highlight(s)}}}}if(KIND!==FIRST||FIRST==='library'||INITIAL_SRC||RESTORED||!rootOf(FIRST))return;const last=recall(KEY+'last');if(last)navigate(viewHref(last,FIRST))}});
previewPageLook(reader.src);syncAppearance();
// A fragment names a dialog to open: #settings, #diagnostics, #history — which is where the old launcher's links land.
{{const h=location.hash.slice(1);if(/^(settings|diagnostics|history)$/.test(h)){{history.replaceState(null,'',location.pathname+location.search);if(h==='history')PANELS.openHistory();else PANELS.openSettings(h==='diagnostics'?'diagnostics':'')}}else PANELS.maybeSetup()}}
</script></body></html>"""

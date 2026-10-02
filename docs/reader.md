# Using the reader

## Use the reader

The app opens on **Library**. Pick a note or page from the sidebar or a card on
the home page, or type a local path or HTTPS URL into its **Open** field. For a
document outside your vaults, set the context folder under that field to the
source material the selected provider is allowed to inspect; notes and artifacts
bring their own.

Then:

1. Select a passage.
2. Right-click the selection.
3. Choose **ELI5**, **Prove it**, or **Ask a question…**.
4. Review the streamed response, tool activity, and evidence cards.
5. Ask a follow-up, open a citation, copy the answer, view prior answers for the
   selection, or hand the conversation to the selected provider in a terminal.

To ask about the page without selecting a passage, right-click its text or blank
space. The question box opens directly, using an excerpt of the page and its
source as context. Links, media, and editable fields keep their usual
right-click menus.

**Prove it** asks the selected provider to verify the passage against the selected folder and
return a Supported, Partially supported, Not supported, or No evidence verdict.

The macOS Selection Service provides the same workflow outside the reader:
select text in another app, open its Services menu, and choose **Ask Selection
with Onyx**. Onyx opens a temporary reading page and selects the
shared passage automatically.

## Editing a note

**⌘E** (or **View ▸ Toggle Editing**) turns a Markdown page into its source, drawn
the way Obsidian's Live Preview draws it: headings, emphasis, code, links, quotes
and bullets look as they will read, and their syntax (`#`, `**`, `[…](…)`) shows
only where the cursor is. A table is drawn as a table and an image as the picture
until you click into one, which shows its source to edit. It opens at the part of
the note you were reading. ⌘E again shows the page, re-rendered, at the same place.

- It saves by itself a moment after you stop typing, and at once on ⌘S, on ⌘E,
  and when you leave the page. The corner shows **Saved**, **Saving…** or
  **Edited**.
- If the file changes on disk while the editor is open (Obsidian, an agent, a
  sync), the new text is taken in when nothing is unsaved. When something is,
  Onyx asks: **Keep mine** or **Use the one on disk**. It never overwrites either
  without asking.
- A link drawn as a link opens on click (⌘-click for a new tab); put the cursor
  next to it with the arrow keys to edit it. ⌘B and ⌘I bold and italicise.
- Tasks (`- [ ]`) have boxes, in the editor and on the page: a click ticks the
  task in the file.
- The outline follows the note while you edit it, and ⌘F searches its text, as
  Obsidian's editor search does: it finds what you typed, markup included.
- It edits `.md` and `.markdown` files that are UTF-8. Both the page and the
  editor read the same Markdown: CommonMark with tables, `~~strikethrough~~`,
  `==highlights==`, tasks, wikilinks and embeds, callouts, `%%comments%%`,
  `#tags` and bare URLs.

## Vault mode

The **Library · Notes · Artifacts** switch at the top of the sidebar (or **File ▸
Library**, ⌘N; **File ▸ Vault**, ⌘⇧V; **File ▸ Artifacts**, ⌘⇧H) changes view in
place. **Notes** is a persistent folder tree of your vault beside the reader;
**Library** shows it and Artifacts together, each under its own heading. Set the
folder under **Settings ▸ Vaults**; it defaults to `~/Documents/CX`. Saving it also adds the folder to the allowed context roots, so
answers can cite the notes themselves — remove it there and the tree keeps
working, but the reader's context folder falls back to the default.

Inside the vault the Markdown reader understands Obsidian's conventions:

| Written | Rendered |
|---|---|
| YAML frontmatter | A collapsible **Properties** block; tags become pills. |
| `[[Note]]`, `[[Note\|alias]]`, `[[Note#Heading]]` | A link resolved by Obsidian's shortest-path rules — the linking note's own folder wins, then the shallowest match. |
| `[[Missing]]` | A dotted span naming the note that does not exist. |
| `![[image.png]]` | The image, preferring the vault's attachment folder. |
| `[text](../Other.md)` | A reader link; relative and percent-encoded paths resolve. |
| `> [!info] Title`, `> [!faq]-`, `> [!tip]+` | A callout in its type's colour and icon, as Obsidian draws it; `-` starts folded, `+` foldable but open. Callouts nest. |
| `%%comment%%` | Nothing: hidden, inline or across lines (to the end of the note if never closed). |
| `#tag`, `#nested/tag` | A tag pill, as in Properties. `#1` and `page#anchor` stay text. |
| `https://example.com` written bare | A link (in any Markdown, not only the vault's). |
| A single newline | A line break, as Obsidian draws a note while *Strict line breaks* is off (its default). Markdown outside the vault keeps CommonMark's soft break. |

Not yet: math (`$…$`), footnotes, Mermaid diagrams, and raw HTML, which the
reader escapes on purpose (Markdown is rendered without executing it).

**Reorganise the vault in the sidebar**, as in Artifacts: drag a note or folder
onto another folder, or onto the tree's empty space for the top; right-click for
**Move To…** (a folder list you can type to filter), **Rename** (a note keeps its
`.md`), and **New Folder** — an empty folder is listed, so it can take a drop.
The same works in the other notes vaults, each within itself. Obsidian rewrites
links only for a move made in Obsidian, so Onyx does it for its own: every
`[text](relative/path.md)` and `![](image.png)` that would break is recomputed in
the same style and encoding, and every `[[link]]` that would now resolve
elsewhere is rewritten to the shortest form that resolves, heading and alias
kept. Code is left alone. Each note is saved against the version read for the
plan, so an edit made meanwhile is never overwritten; the toast names any note
it could not update, and notes inside a linked folder (another tree's files) are
reported rather than written. A page's asks, highlights and reading position
follow it to its new path, in Artifacts too.

Clicking a link swaps the reader pane and moves the tree highlight; the browser
Back button walks the history. Press `/` to focus the filter box, Escape to clear
it. The pin beside the vault's name (or ⌘\, even with focus inside the page)
unpins the sidebar, as in Zen's compact mode: the reader gets the whole window,
and the sidebar floats out over it when the pointer rests on the window's left
edge, going again once the pointer leaves. It stays out while it is in use — a
row's menu or the **+** panel open, or typing in the filter — and `/` brings it
out to type in. Pin it to dock it again; the choice is remembered.
Symlinked vault folders are followed and keep their vault-visible paths, so
links between notes inside them stay in the vault.

On the reader's right is the **Outline**, as in Obsidian: the page's headings,
nested by level, for any note, artifact, or other document that has some. It
rests as a small round icon in the reader's top-right corner; rest the pointer on
the reader's right edge, or hover or click the icon, and the outline floats out
over the page, going again once the pointer leaves it. Click a heading to scroll the page to it, and the section you are
reading stays marked as you scroll. A twisty folds a section, the button in the
header folds or unfolds them all, and the filter box keeps matching headings
with their parents. The pin in its header (or ⌘⇧\, also from inside the page)
docks it as a third column beside the reader; that is remembered too. A window
too narrow for three columns keeps it floating.

The same panel's other half is **Related**: the pages nearest the one you are
reading, by meaning rather than by any link you made. It reads the vault MCP's
index — the one ⌘P searches — from the page's own direction instead of a typed
query, so it embeds nothing, asks nothing of Ollama, and answers in about the
time a search takes. Notes and Artifacts are searched together, so a note can
turn up an artifact and the other way round.

A bare cosine won't do here, and the reason is worth knowing. This model puts a
strong common direction into every vector: two unrelated passages still score
0.635 against each other, and the pages lying nearest that direction — the long,
diffuse ones, a playbook or a meeting transcript — come back as the neighbours
of everything. So the baseline is measured once per index and taken back out of
every score. What's left means *closer than any two pages in this vault are
anyway*: 0 is a stranger, 1 is the same text. That number is comparable between
pages, which is what lets the pane drop anything below a floor — so everything
listed is related by design, and a page whose subject appears nowhere else says
so instead of padding itself out with its own tail.

The number on a row is that score shown on the range Smart Connections uses, so
the two panes read alike: 0.70 is the least related page worth listing, 1.00 the
same text. The panes embed with different models, so a note's two numbers land
in the same range rather than on the same value.

The map above the list draws the neighbourhood around the page you are on, each
dot with its score beside it. Distance from the centre is the score, stretched
over this page's neighbours — the nearest on the inner ring, the furthest on the
outer — and the angle puts the neighbours that are related to one another
together, so pages on one subject sit in one place. It is a radial stress layout:
measured on 80 pages, its distances between dots agree 0.65 with how related the
pages are, where Smart Connections' layout manages 0.36. Click a dot or a row
to open it. A neighbour near enough to be the same content twice is marked
`same?` — usually a note beside its own HTML rendering, or an inbox capture
beside the note it became.

Right-click a note or folder for its menu: **Open**, **Reveal in Finder** (the
real file), and — for anything reached through a symlink — **Reveal Link in
Finder**, which shows the link itself in the vault folder. Hold ⌥ and they read
**Copy Path** and **Copy Link Path**: the resolved absolute path, or the path
through the link. Like cxtasks, Onyx draws its menus in its own theme; in the app,
WebKit's stock menu appears only in text fields and on a document's own links,
media, and selected text.

Hover a page (or Tab to it) and a card beside the sidebar shows its whole title,
its one-line summary (a page's description, else its subtitle, else its first
paragraph), where it lives, and when it last changed.

With the Obsidian plugin running, **Settings → Appearance → Match vault
appearance** (on by default) dresses the whole app in your vault's theme. The
sidebar wears its file explorer — font, colors, chevrons, indent guides and
per-folder colors. Notes, and the pages Onyx lays out itself (plain text, PDFs,
shared selections), take its reading styles. Everything else — the main pane,
Library's home page, Settings and Recent conversations, menus, the answer panel,
and the window's own light or dark — takes a palette drawn from the vault's
ground, ink, link and code colors, in its interface font. Text shades are mixed
from the vault's ink to fixed contrasts, so no theme leaves a label unreadable.
Color theme follows the vault while the switch is on. Authored HTML keeps its
own look. See the plugin's README.

## Artifacts

**Artifacts** (on the switch at the top of the sidebar, or **File ▸ Artifacts**,
⌘⇧H) browses a folder of **symlinks to HTML pages anywhere on your Mac** — the HTML counterpart of an Obsidian vault. It defaults to
`~/Documents/Artifacts`; change it under **Settings ▸ Vaults**.

- **Folders nest as deep as you like.** A folder you made is listed even while
  empty, so there is somewhere to add or drag pages; a linked folder is listed
  once a page sits somewhere beneath it. Link a whole folder (say a topic in
  `~/learnings`) and every page added to it later shows up on its own.
- **Pages are listed by their `<title>`**, with how long ago each changed. Below
  the top level a folder containing `index.html` is one page, so a guide
  folder reads as a single entry; its `index.inline.html` twin, audio, and notes
  stay out of the list.
- **Reorganise in the sidebar.** Drag a row onto a folder (or onto the list's
  empty space for the top level); a shut folder held under the pointer springs
  open. Right-click for **New Folder**, **Rename**, **Move To…**, **Pin to Top**,
  and **Remove from Artifacts**. Renaming a page changes the name it is shown by,
  not its `<title>` — the file is the link's target, somebody else's — and
  **Use Page Title** puts the title back. Moving a link moves only the link — its target stays
  where it is — and a hand-made relative link that a move would re-aim is
  rewritten as an absolute one. Only what Artifacts owns can be handled: a link
  or a folder sitting in one of its own folders, never a page inside a linked
  folder (that page lives in another tree). Remove takes out a link or an empty
  folder, never a target. A pin, and a page's own name, live in the folder's
  hidden `.onyx.json` and move with their entry.
- **A link whose target is gone stays listed, struck through, as *missing*.**
- **+** links more in: HTML files or a folder through the native picker, or a
  pasted path or `file://` URL, into a project you choose; it can also create
  folders. A linked `index.html` is named after its folder.
- Questions use **the real folder behind the link** as their context — a page
  under a linked topic folder can cite that topic's notes. Linking something adds
  that folder to the allowed context roots (never your home folder or `/`);
  remove it in Settings to take the access back.

Guides that narrate with `<audio src="audio/….m4a">` play in the reader: media
tags are rewritten to document capabilities, and `/_fs` answers byte ranges,
which WebKit requires before it will play media. A `file://…/index.html#section`
link keeps its fragment, and the fragment wins over the remembered scroll
position.

## Supported documents

| Source | Behavior |
|---|---|
| Local HTML/HTM | Re-served from localhost with authored scripts and buttons enabled; referenced local assets receive an expiring document capability. |
| Markdown | Rendered offline by the built-in HTML-escaping renderer. |
| Plain text | Displayed in a selectable reading view. |
| PDF | Extracts selectable text per page with `pypdf`; page-aware citations are preserved. |
| HTTP/HTTPS HTML | Fetched with redirect, content-type, size, and private-network checks. |

PDFs that contain only scanned images need OCR before Onyx can select or
reason over their text.

## Opening a document directly

```text
http://127.0.0.1:8899/view?src=/absolute/path/notes.md&folder=/absolute/path/project
http://127.0.0.1:8899/view?src=https://example.com/article
```

The reader makes the page same-origin with the local service, rewrites referenced
assets, removes the source page's CSP, and injects the widget. Trusted local HTML
retains authored scripts and inline handlers under a local-only connection policy;
remote HTML has its scripts removed. This avoids `file://` fetch and storage
restrictions while keeping downloaded web pages inert.

Because local HTML runs with the local reader origin, open interactive HTML only
when you trust its contents, just as you would before running a local script.

For a trusted page you control, the original standalone embed still works:

```html
<script src="http://127.0.0.1:8899/ask.js"></script>
```

Serve the page over HTTP rather than `file://` for predictable browser behavior.

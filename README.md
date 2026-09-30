# Onyx

Onyx (formerly Ask Widget) is a local-first reading workspace for Claude Code and Codex. Open an HTML,
Markdown, text, or PDF document; select a passage; then right-click for **ELI5**,
**Prove it**, or **Ask a question**. Answers stream into the document and can use
read-only evidence from a context folder you choose.

![Highlight a passage, pick ELI5, and the answer streams into the page with evidence from your folder (illustrative content)](docs/media/onyx-demo.gif)

<sub>Illustrative article and answer.</sub>

## Why

Asking an AI about something you are reading usually means copying the passage
into a chat and losing your place. Onyx brings the question to the passage:
answers stream into the page, grounded in a folder of source material you
choose, with citations that are checked to exist before they are shown. It runs
on your Mac against your own files, on the Claude or ChatGPT subscription you
already have, never an API key.

```text
selection → Onyx → local FastAPI service → Claude CLI (claude.ai subscription)
                ↑              │               ↘ Codex CLI (ChatGPT subscription)
                └──── answer, tool trace, validated citations ───────────────┘
```

## Quickstart

Download the latest `Onyx-<version>-macOS-arm64.dmg` from
[Releases](https://github.com/cxrobx/onyx/releases/latest), open it, and drag
Onyx to Applications. The app is signed and notarized. It runs on Apple Silicon
Macs with macOS 13 or newer.

On first launch Onyx opens **Settings ▸ Setup**, a checklist with a fix beside
each step:

1. **Claude or Codex, signed in.** Install the
   [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) and run
   `claude` once to sign in with a paid claude.ai plan, or install the Codex
   CLI and sign in with ChatGPT. Onyx never uses an API key.
2. **Obsidian vault.** Onyx picks the vault Obsidian has open. Choose another
   one here if you like.
3. **Artifacts folder.** Onyx creates `~/Documents/Artifacts` for HTML pages.
4. **Onyx plugin in Obsidian.** Press **Install plugin**, then quit and reopen
   Obsidian. If Obsidian shows Restricted mode, turn it off in Settings ▸
   Community plugins.
5. **Vault look.** Arrives from the plugin within a few seconds, and Onyx then
   matches your vault's colours and fonts.

Setup stops opening on its own once every step passes, or after you press
**Don't open Setup on launch**. It stays in Settings either way.

**Settings ▸ Appearance ▸ Match page appearance (HTML)** is on by default. The
sidebar, tabs and answer panel follow the HTML page’s colours while keeping the
interface font. Notes, image backgrounds and pages without readable solid colours
keep the vault or Onyx appearance. Turn it off to keep that appearance everywhere.

If you use Alfred, download
[`Open-in-Onyx.alfredworkflow`](https://github.com/cxrobx/onyx/releases/latest/download/Open-in-Onyx.alfredworkflow) from the same
release and double-click it. It adds `onx` and `onxc` search and an **Open in
Onyx** action; see [docs/integrations.md](docs/integrations.md#open-from-finder-or-alfred).

To build the app yourself, or run the service from a checkout, see [docs/install.md](docs/install.md).

Then open a document from **Library**, select a passage, right-click, and choose
**ELI5**, **Prove it**, or **Ask a question…**.

## What it does

Three actions on any selection, or on a whole page when nothing is selected:

- **ELI5** explains the passage plainly.
- **Prove it** verifies the passage against your context folder and returns a
  Supported, Partially supported, Not supported, or No evidence verdict.
- **Ask a question…** answers anything about it, with follow-ups.

Around them:

- Readers for HTML, Markdown, plain text, and text-based PDF, plus HTTPS pages.
- **⌘E edits a note in place**, as in Obsidian: the page turns into its Markdown
  drawn as Live Preview, saves as you type, and turns back with ⌘E.
- **Notes** and **Artifacts** sidebars: your Obsidian vault (wikilinks,
  frontmatter, outline, related pages) and a folder of links to HTML pages
  anywhere on your Mac.
- Streaming answers with a visible trace of every file read and web lookup, and
  evidence cards that open the cited source lines.
- Search (⌘P), find in page (⌘F), and a searchable history of past questions you
  can ask again, edit, or continue.
- Claude or Codex, with live model pickers, on your subscription only.
- An Obsidian plugin, an Alfred workflow, and a macOS Service, so the same
  actions work outside the app.

## Docs

| Doc | What's in it |
|---|---|
| [docs/features.md](docs/features.md) | The full feature list and recent version notes |
| [docs/install.md](docs/install.md) | Building the app from source, what the launcher does, running from a checkout, running headless, CLI options |
| [docs/reader.md](docs/reader.md) | Using the reader, Vault mode, Artifacts, supported documents, opening a document by URL |
| [docs/integrations.md](docs/integrations.md) | Finder, the macOS Services, the Alfred workflow and its `onx`/`onxc` search, the Obsidian plugin |
| [docs/storage.md](docs/storage.md) | Library, the database and what it saves, context roots, trusted roots |
| [docs/security.md](docs/security.md) | The security model of the local service |
| [docs/development.md](docs/development.md) | Test and build commands, release signing and notarization, project layout |
| [AGENTS.md](AGENTS.md) | Invariants and deliberate old names, for anyone changing the code |

## Contributing with an agent

Point your coding agent at [`AGENTS.md`](AGENTS.md) before it changes anything.
It is short on purpose: the invariants that must not be relaxed, the one command
that actually runs the suite, and the names that look stale but are load-bearing
contracts. Claude Code, Cursor, and Codex all read it automatically.

## License

[Business Source License 1.1](LICENSE), © 2026 CX Ventures LLC. The source is
available and you may use it personally or inside your own organisation.
Selling it, hosting it for others or bundling it into a commercial product
needs a commercial licence. Each version becomes Apache-2.0 on 2030-09-30 or
four years after its release, whichever comes first.
Versions published before 2026-09-30 were released under the MIT licence.

The project was renamed from **Ask Widget** to **Onyx**; the licence is
unchanged by that, and the built wheel carries
`License-Expression: BUSL-1.1` with `LICENSE` bundled. No third-party code is
vendored here, and every runtime dependency (FastAPI, uvicorn, markdown-it-py,
pypdf) is MIT- or BSD-licensed.

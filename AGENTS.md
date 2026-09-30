# AGENTS.md

Read this before changing anything. The README explains what Onyx *is* and how to
install it; this file is the part a coding agent gets wrong.

Onyx is a local-first reading workspace: a small FastAPI service on `127.0.0.1`
serves documents into a WebKit reader, injects `static/ask.js` into them, and
streams answers from the **Claude** or **Codex** CLI. A native Swift app
(`launcher/Onyx.swift`) wraps it. It runs on the user's own machine, against the
user's own files, so every boundary in here is a real boundary.

## Verify with this

```bash
PYTHONPATH=src python -m unittest discover -s tests     # ~200 tests, ~5s
```

That is what CI runs, and it is the only command guaranteed to work — there is no
`pytest` in the runtime environment, and a `pytest` on `PATH` usually lacks
`fastapi`. The rest of CI, worth running when you touch those surfaces:

```bash
swiftc -typecheck -framework Cocoa -framework WebKit \
  -framework UniformTypeIdentifiers launcher/Onyx.swift
plutil -lint launcher/Info.plist
cd integrations/obsidian && npm ci && npm run check && npm run build && npm test
cd editor && npm ci && npm run check && npm run build && git diff --exit-code ../static/onyx-editor.js
PYTHONPATH=src python -m unittest tests.browser_smoke     # needs playwright chromium+webkit
```

**A passing build is not a passing feature.** The browser suite drives real
Chromium and WebKit; reach for it when you change `static/ask.js` or anything the
reader renders. Note the shipped macOS app runs an **older system WebKit** than
Playwright's, so a layout bug can pass both Playwright engines and still be wrong
in the app.

## Invariants — do not relax these

Each one has a test. If your change makes a test here fail, the change is wrong,
not the test.

**The filesystem boundary.** `/_fs` serves *only the exact files a viewed document
referenced*, each behind a per-document capability token. It is not a
home-directory reader. Path checks resolve symlinks **before** comparing against
allowed roots, and compare path components — not string prefixes, or
`/home/user-evil` passes a `/home/user` check.
→ `test_document_assets_use_a_capability_url`,
`test_allowlist_rejects_a_sibling_with_the_same_prefix`,
`test_allowlist_follows_symlinks_before_checking_boundaries`

**Scripts run only in trusted local HTML.** Remote and untrusted documents get
their `<script>` tags stripped in `viewer.prepare_html`, and a remote document can
never opt back in. Markdown is rendered without executing raw HTML.
→ `test_remote_html_scripts_stay_inert_even_if_requested`,
`test_trusted_local_html_keeps_interactive_scripts`,
`test_markdown_is_rendered_without_executing_raw_html`

**Remote fetches refuse private addresses.** SSRF guard on the URL reader.
→ `test_remote_private_addresses_are_rejected`

**Mutations need the server token; only allowlisted origins get one.**
→ `test_mutations_require_the_server_token`,
`test_session_endpoint_returns_token_only_to_allowed_origins`,
`test_unlisted_origins_are_still_rejected`

**The model never gets write tools.** Provider commands gate web access and allow
no writes, ever. Adding a write tool to the runner is not a feature.
→ `test_claude_command_gates_web_tools_and_never_allows_writes`

**A save writes only the note its page was opened from, and never over a version
nobody saw.** ⌘E's editor gets a note's source only against that page's own
document capability, and an edit capability for that one file with it;
`POST /api/source` takes no path. It writes an existing UTF-8 Markdown file in
place, and refuses a save written against an older version than the one on disk
(409), so neither side's edit is lost. This is the user's write path; it does
not loosen the model's. The rules an adversarial review (Codex, 2026-09-25) found
missing, each now with a test:
- the version check and the write are one step under a per-note lock
  (`viewer._note_lock`), so of two saves against one version the second is refused;
- the capability's path must still be its own realpath, before and after the
  open: `O_NOFOLLOW` alone guards only the last component, and a folder swapped
  for a symlink led the same path to another note;
- a save whose file was replaced under it (a sync renaming its copy in) is a
  conflict, never "Saved": the bytes went to a file the path no longer names;
- a task tick is made against the version the page *shows* (`askw-doc-sig`), not
  the newer one live reload has seen while a reload waits;
- text that may not have reached the file when its page goes is kept as a draft
  and offered back on the next ⌘E; "Keep mine" always writes.
The editor's images come through `/_fs` under their own capability, which lists
only images the note *as saved* references, so `/_fs` still serves only files a
document referenced.
→ `test_editing_saves_only_the_note_its_page_opened_and_never_over_a_newer_version`,
`test_two_saves_against_one_version_never_both_land`,
`test_a_note_is_saved_only_where_its_page_found_it`,
`test_a_note_replaced_while_it_is_saved_is_a_conflict_not_a_save`,
`test_a_tick_is_made_against_the_version_the_page_shows`,
`test_no_edit_is_lost_or_misfiled_when_the_note_moves_under_it`,
`test_the_editor_draws_only_images_the_saved_note_references`,
`test_a_task_box_on_the_page_ticks_its_line_in_the_note`,
`test_saving_a_note_keeps_its_file_its_line_endings_and_its_byte_order_mark`,
`test_a_note_edited_through_a_linked_folder_saves_to_its_real_file_and_follows_its_links`

**Vault mutations stay inside the vault.** Link, folder and reorganise routes
write only within the vault and never through a symlink target.
→ `test_link_and_folder_routes_write_only_inside_the_vault`,
`test_reorganising_moves_only_what_the_vault_owns_and_never_a_target`

**The phone mirror is inert, fenced and sealed.** `src/onyx/mirror/` publishes an
encrypted, read-only copy of the vault for a phone (`docs/plans/phone-mirror.md`,
whose "Wire format v1" the Mac, the Worker in `integrations/mirror-worker/` and the
iOS app all implement). This repo is public, so it does nothing unless the real
entry point runs (`AppConfig.mirror`) *and* `mirror.toml` in the data dir says
`enabled = true` with a non-empty `include`. A page is published only if the vault
index lists it under an included path; an asset only if a published page references
it and it lives under the home folder. Every byte and every object name leaving the
Mac is encrypted or HMAC'd. Credentials live only in the Keychain (service
`onyx-mirror`, written through `security -i` on stdin, never argv) and never reach
settings, diagnostics, logs or reports. No endpoint, bucket or token is ever
tracked. The Worker answers only an authenticated `GET`/`HEAD /o/<id>`, and 404s
everything else.
→ `test_mirror_is_inert_without_its_config`,
`test_mirror_publishes_only_included_folders`,
`test_mirror_uploads_only_assets_a_page_references`,
`test_mirror_assets_stay_inside_home`,
`test_mirror_sends_no_plaintext`,
`test_mirror_credentials_never_reach_settings_or_logs`,
`test_keychain_writes_keep_values_off_argv`,
`test_repo_tracks_no_private_endpoint`,
`test_mirror_crypto_matches_the_shared_vectors`, and the Worker's
`node --test` in `integrations/mirror-worker`

**Subscription-only execution.** Claude runs through a signed-in claude.ai
session and Codex through ChatGPT. API-key environment variables are stripped and
non-subscription sessions are rejected, deliberately, so the app can never bill
the user per token. Do not add an API-key path.

**Version metadata moves together.** `src/onyx/__init__.py`, the Obsidian
`manifest.json` and `versions.json`, and the release metadata must agree.
→ `test_all_release_metadata_uses_the_same_version`,
`test_obsidian_manifest_and_versions_json_agree`

## Names that look stale but are contracts

The project was **Ask Widget** before it was Onyx. Some old names survive on
purpose. Renaming any of these breaks existing users with nothing to show for it:

| Name | Where | Why it stays |
|---|---|---|
| `askw-` / `askw:` | ~930 uses: CSS classes, `data-askw-*`, `<meta name="askw-…">`, `localStorage["askw:folder"]` | A wire contract baked into every served page, the Obsidian plugin, and users' saved browser state |
| `~/Library/Application Support/Ask Widget/ask-widget.db` | `storage.legacy_database()` | The **migration source**. Rename it and existing users' history is stranded |
| `ask-widget-panel`, `ask-widget-modal`, `VIEW_TYPE_ASK_WIDGET` | `integrations/obsidian/` | Obsidian persists the view type in the user's workspace layout; the CSS classes may be in their snippets |
| `html_vault_root`, `kind="html"`, `?vault=html` | storage, routes | Predate the "Artifacts" name. Internal, and not worth a migration |
| `~/Projects/ask-widget` | `launcher/Onyx.swift`, `scripts/onyx-daemon.sh` | The checkout folder kept its old name; both spellings are accepted |

The user-facing name is **Onyx** everywhere else — repo, package, app, wheel.

## Decisions an agent tends to "fix"

**`markdown_theme.KINDS` excludes `"html"` on purpose.** Onyx pushes the vault's
measured Obsidian styles into notes, text and PDF — the pages it lays out itself.
An authored HTML page keeps its own look, and it still follows the vault, because
a page from the HTML Artifact Kit carries its own `prefers-color-scheme` block and
WebKit resolves that from the window's `NSAppearance`, which the shell sets from
the app theme or the vault's mode. Adding `"html"` to `KINDS` would fight the page
instead of helping it.

**The reader is an iframe, same-origin, and navigation is plain HTML.** The
sidebar's rows are `<a target=reader>` links, so the browser navigates and keeps
history and back/forward. Don't replace it with router JS. Each open tab is an
iframe of its own (`tabs_ui.py`), and a frame keeps the name it was born with:
the shell's one click listener points a `target=reader` link at the frame
showing. Don't "simplify" that into renaming the frames. The app's WebKit 17
files joint history under frame names, so after a rename Back loads one tab's
page into another, while Chromium and Playwright's WebKit pass.

**The back/forward swipe is WebKit's, with a cover held over its end.** WebKit
lifts its swipe snapshot once the main frame has painted, but every Back and
Forward here moves the reader frame, so the page just left used to flash after
each swipe. `SwipeCover` in `launcher/Onyx.swift` holds a picture of the
destination until the shell sends `askwPainted` (`tellPainted` in `tabs_ui.py`,
a frame after each reader load or popstate). `tellPainted` has no JS caller, so
it looks dead; it isn't. Keeping the slide, rather than an instant swipe without
WebKit's snapshot, was the owner's call (2026-09-22).

**`static/onyx-editor.js` is a build, and it is committed.** Edit `editor/src/`,
then `npm run build` in `editor/`; CI fails on a bundle that doesn't match its
source. It is a separate file so `ask.js` stays one dependency-free script, and it
loads only on the first ⌘E. The editor follows the reader's Markdown (CommonMark,
tables, strikethrough, `==highlights==`, tasks, wikilinks) rather than all of
Obsidian's, on purpose: ⌘E should never show a construct styled that the page it
toggles back to leaves as plain text. Add a construct to the reader
(`viewer._build_markdown`) and the editor (`editor/src/preview.ts`) together.

**While the editor is open, the outline and ⌘F read it, not the page.** The page's
rendering is hidden under it, and CodeMirror draws only the lines near the window,
so the shell asks the editor (`window.askwEditor`): headings with their places,
and the note's text to search (source, markup included, as Obsidian's editor
search does). `headingsOf` and `find_ui.collect` branch on `editorOf(doc)`.

**A save writes the note in place, not to a temp file renamed over it.** A rename
gives the file a new inode and creation date, and Obsidian shows and sorts notes by
that date. `viewer.write_source` writes the new bytes over the old, then truncates.

**Comments here carry reasons, not restatements.** Several explain a measurement
or a failure that motivated the code. If you change such code, update the reason
or delete it — do not leave a comment describing behaviour that no longer exists.

## Layout

`src/onyx/app.py` is the HTTP surface and orchestration; `viewer.py` the secure
readers and `prepare_html`; `vault.py` the note/HTML index; `storage.py` SQLite;
`claude_runner.py` / `codex_runner.py` the provider processes and SSE translation;
`*_ui.py` the server-rendered shell. `static/ask.js` is the injected widget — the
reusable core if you are building something similar. `editor/` builds the ⌘E
editor into `static/onyx-editor.js`. `launcher/` is the Swift app,
`integrations/` the Obsidian plugin and Alfred workflow.

## Scope

Small, verified changes. Commit when a unit of work is complete *and* checked;
don't bundle unrelated fixes. If you find a second problem, say so rather than
silently widening the change.

Licensed MIT — see `LICENSE`. Contributions are under the same terms.

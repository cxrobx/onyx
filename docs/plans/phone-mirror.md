# Plan: a read-only phone mirror

Status: proposed, 2026-09-30. Nothing here is built yet.

Read your notes and Artifacts on an iPhone without Obsidian Sync. The Mac publishes
an encrypted copy to free object storage, and a small iOS app keeps a copy of its
own and reads it offline. The phone never writes back, so there is nothing to merge:
the Mac's vault is the only version, and the phone shows its last copy.

**This repo is public.** The feature ships in Onyx's code, so it has to be safe to
publish that code: it does nothing on anyone's Mac, the owner's included, until it
is deliberately turned on, and no destination, credential or vault content ever
lands in a tracked file. The gates below are the design, not an add-on.

## Shape

```
Mac (Onyx)                        Cloudflare                     iPhone
─────────────                     ──────────                     ──────
vault + Artifacts                 R2 bucket (private)            Onyx Mirror app
  │ build pages (viewer.py)         holds only ciphertext,         fetch index
  │ encrypt each file               objects named by opaque ids    download changed ids
  │ upload changed objects ──────▶                                 decrypt on read
  │   (write-only R2 key)         Worker: GET /o/<id> + token ◀──  (read-only token)
```

- **Storage: Cloudflare R2.** Free tier is 10 GB stored and free downloads; the
  copy is a few hundred MB. No machine to maintain.
- **Worker, about 20 lines.** Checks the phone's bearer token and returns one
  object by id. Free up to 100,000 requests a day.
- **Encryption: AES-256-GCM**, done on the Mac, undone on the phone (CryptoKit's
  `AES.GCM.SealedBox(combined:)` reads the same nonce‖ciphertext‖tag layout
  Python's `AESGCM` writes). Cloudflare sees neither contents nor names.

## The gates

Each gate gets a test, listed under Invariants. A change that makes one fail is
wrong, as with the rest of `AGENTS.md`.

1. **Inert without its config file.** The mirror runs only when
   `~/Library/Application Support/Onyx/mirror.toml` exists and says
   `enabled = true`. No default turns it on, it has no row in the SQLite
   `settings` table, and the Settings pane shows nothing about it unless that file
   exists. Without the file: no network call, no files written, no background task.
2. **Its dependency is opt-in.** Encryption needs `cryptography`, which Onyx does
   not depend on today. It goes in a `mirror` extra in `pyproject.toml`
   (`pip install onyx[mirror]`), imported lazily inside the mirror module, so a
   normal install doesn't even contain the code path's requirements.
3. **An explicit include list, never "the whole vault".** `mirror.toml` names the
   folders to publish. There is no default list. Always excluded: dot-folders
   (`.obsidian`, `.trash`, plugin caches such as `.smart-env`), and any note whose
   frontmatter says `mirror: false`. An included folder's symlinks are followed
   only to targets that are themselves inside an included root, checked after
   `resolve()` and by path components, as `config.resolve_allowed` already does.
4. **Assets: only what a page references.** A page's images and files go up only
   if the page itself references them, the same boundary `/_fs` enforces
   (`prepare_html`'s asset sink). The mirror never uploads a folder wholesale.
5. **Credentials live in the Keychain, nowhere else.** The R2 write key, the
   Worker URL and the encryption key are stored under Onyx's own Keychain service
   (`onyx-mirror`) by a setup command that reads them from a prompt, never from
   argv. They never appear in `mirror.toml`, the settings table,
   `/api/settings`, `/api/diagnostics`, logs or error pages.
6. **Nothing readable leaves the Mac.** Every object is encrypted, the index
   included. Object names are an HMAC of the page's path under a key of their
   own, so a folder or client name never reaches Cloudflare as a filename.
7. **The Worker refuses by default.** The bucket has no public `r2.dev` access.
   The Worker answers only `GET /o/<64 hex chars>` with a valid bearer token
   (constant-time compare), has no listing route, and returns 404 for everything
   else. The phone's token can only read; the Mac writes with a separate R2 key
   scoped to that one bucket.
8. **No private endpoint in the repo.** The Worker's `wrangler.toml` (bucket
   name, account id, route) is gitignored; only `wrangler.example.toml` is
   tracked. A test scans tracked files for `*.workers.dev` hosts, R2 account
   endpoints and bearer-shaped strings, and fails on anything but the
   placeholders. Test fixtures are synthetic vaults only.
9. **A kill switch.** `onyx mirror stop` turns publishing off. `onyx mirror wipe`
   deletes every object in the bucket. Rotating the encryption key makes every old
   copy, anywhere, unreadable. Revoking the phone token locks the app.

## Pieces

| Piece | Where | Public? |
|---|---|---|
| Publisher, crypto, uploader | `src/onyx/mirror/` | Yes (generic, inert by default) |
| Worker | `integrations/mirror-worker/` (+ `wrangler.example.toml`) | Yes (no config) |
| iOS app | its own private repo | No |

**Publisher.** For each included page:
- Markdown goes through `viewer.load_local_document(path, display_path=…,
  vault=index)` with the vault's Markdown theme CSS added, as `/view` does, so
  the page matches the app. Its `RenderContext.view_url` is overridden to point
  links at mirror ids instead of `/view?src=…`, which covers wikilinks and
  relative links, since both go through it.
- Artifacts HTML is published as the author wrote it (scripts kept, as trusted
  local HTML already is), with its asset references rewritten to mirror ids
  through the same `_ASSET_RE` pass `prepare_html` uses. No `ask.js` is added.
- The index (encrypted, one object) lists every page's id, title, folder, kind,
  size and a hash of its encrypted bytes, plus a plain-text extract per page for
  search on the phone.
- It re-publishes a page when its built output's hash changes (so a theme change
  counts), uploads only changed objects, and deletes the objects the new index no
  longer lists. It runs every few minutes while enabled, plus a *Publish now*.
- Upload is stdlib `urllib` plus a small SigV4 signer. No `boto3`.

**iOS app (SwiftUI).**
- Fetches the index (with ETag), downloads only ids whose hash changed, deletes
  what the index dropped.
- Stores the encrypted objects as they came. A `WKURLSchemeHandler` for
  `onyx-mirror://` decrypts each one when the reader asks for it, so relative
  links and images work and no plaintext is written to disk.
- Sidebar tree, title and full-text search over the index, dark mode, and a
  "published 12 min ago" line so a stale copy says so.
- The reader's CSP allows no network connections: pages read offline and can't
  phone home.
- Goes onto the phone by direct Xcode device install; no TestFlight.

## Phases

1. **Gates, crypto and publisher, to a local folder.** No network. All
   invariant tests below pass; a synthetic vault publishes, decrypts back and
   matches.
   *Risk:* link rewriting diverges from the reader's. *Handle:* build through
   `viewer`'s own renderer with an overridden `view_url`, never a second renderer.
2. **R2 and Worker.** Create the bucket (no public access), the Worker, the
   scoped write key and the read token. *Verify:* `curl` without a token → 404,
   with a token → ciphertext only, no listing route.
   *Risk:* a key set as a command argument or printed. *Handle:* keys go in with
   `wrangler secret put` and the Keychain prompt, and are never echoed.
3. **iOS app v1.** Index sync, decrypt, sidebar, reader, offline.
   *Risk:* Artifacts pages with scripts or relative assets break in the scheme
   handler. *Handle:* test the heaviest page (narrated explainers with inlined
   audio) first.
4. **Search and polish.** Full-text search, staleness line, wipe/lock flows.

## Invariants (tests to add)

- `test_mirror_is_inert_without_its_config` (no socket, no file, no task)
- `test_mirror_publishes_only_included_folders` (dot-folders, `mirror: false`
  notes and symlinks escaping the include roots stay out)
- `test_mirror_uploads_only_assets_a_page_references`
- `test_mirror_sends_no_plaintext` (a canary string in a note's body, title,
  filename and folder name appears in no uploaded byte and no object key)
- `test_mirror_credentials_never_reach_settings_or_logs`
- `test_repo_tracks_no_private_endpoint`
- Worker: refuses without a token, has no listing route, serves only well-formed ids

Once built, these join the Invariants section of `AGENTS.md`.

## Open decisions (the owner's)

- Which folders go in the include list. Client folders are the ones to decide on
  deliberately; encryption makes including them safe, but it's still a copy on a
  phone.
- Whether the iOS app stays private (the plan's default) or joins this repo.
- How often to publish (every 5 minutes is the default here).

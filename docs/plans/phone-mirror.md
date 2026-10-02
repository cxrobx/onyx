# Plan: a read-only phone mirror

Status: built 2026-09-30 (Mac publisher, Worker, iOS app in its own private repo). Phases 1–4 done; still to confirm on the phone: the QR scan and a first sync against the live Worker.

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
   paths to publish. There is no default list. A page is published only if
   Onyx's own vault index (`vault.VaultIndex`, what the sidebar lists) has it
   under an included path, so the mirror never walks the disk on its own and
   follows exactly the links the vault already follows: an Artifacts entry is a
   symlink the owner made on purpose, and a linked folder in the notes vault is
   one the sidebar already shows. Always excluded: anything under a dot-folder
   (`.obsidian`, `.trash`, plugin caches such as `.smart-env`), any path under an
   `exclude` entry, and any note whose frontmatter says `mirror: false`.
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

## Wire format v1

The Mac, the Worker and the phone agree on exactly this. Changing any of it is a
new version (`v` in the index, the salt below, and the pairing prefix together).

**Keys.** One random 32-byte master key `M`. Two keys derive from it with
HKDF-SHA256, salt `b"onyx-mirror/v1"`, 32 bytes each:
`K_enc` with info `b"enc"`, `K_id` with info `b"id"`.

**Object ids.** `id(name) = hex(HMAC-SHA256(K_id, utf8(name)))`, 64 lowercase
hex characters. Names:
- `index` for the index;
- `page:` + the page's mirror path, which is `Notes/<path in the vault>` or
  `Artifacts/<path of its entry in Artifacts>` (POSIX separators);
- `asset:` + the asset file's realpath.

**Blobs.** `blob = nonce(12 random bytes) ‖ AES-256-GCM(K_enc, nonce, plaintext,
aad = ascii(id))`, the ciphertext followed by its 16-byte tag. This is Python
`AESGCM(K_enc).encrypt(nonce, data, id.encode())` with the nonce prepended, and
CryptoKit `AES.GCM.SealedBox(combined: blob)` opened with
`authenticating: Data(id.utf8)`. The id as AAD binds each blob to its name, so a
store that swaps two objects makes both fail to open.

**Storage and fetch.** R2 key `o/<id>`. The Worker serves `GET`/`HEAD`
`/o/<id>` with `Authorization: Bearer <read token>`: 200 with the blob, an
`ETag`, `Cache-Control: no-store`; 304 on a matching `If-None-Match`; 404 for
everything else, a bad or missing token included.

**Index.** The plaintext is raw DEFLATE (RFC 1951, no zlib header: Python
`zlib.compressobj(wbits=-15)`, Apple `NSData.decompressed(using: .zlib)`) of
UTF-8 JSON:

```json
{
  "v": 1,
  "published_at": "2026-09-30T12:00:00Z",
  "pages": [{"id": "…", "path": "Notes/Folder/Note.md", "title": "Note",
             "kind": "markdown", "sha": "…", "size": 1234,
             "mtime": 1790000000.0, "text": "plain text for search"}],
  "assets": [{"id": "…", "mime": "image/png", "sha": "…", "size": 5678}]
}
```

`kind` is `markdown`, `html` or `text`. `sha` is SHA-256 hex of the **blob**
(what the phone stores), `size` its byte length. `text` is at most 20,000
characters. A blob is re-encrypted only when its plaintext changes, so an
unchanged page keeps its `sha` and the phone doesn't download it again.

**Look (optional, added 2026-09-30).** The index may carry `"look"`, so the phone
wears what the Mac app wears. A phone that doesn't know it ignores it, so it is still
v1:

```json
"look": {
  "vault": {"mode": "light", "base": [253, 246, 227], "tokens": {"--bg-primary": "253 246 227", "…": "…"}},
  "vaults": {"light": {"mode": "light", "…": "…"}, "dark": {"mode": "dark", "…": "…"}},
  "follow_page": true,
  "appearance": "system"
}
```

- `vault` is `vault_look.palette(markdown snapshot, sidebar snapshot)` exactly as the
  Mac computes it (`current_vault_look`): `mode`, `base`, and `tokens`, whose values
  are space-separated RGB triplets (`"253 246 227"`), `rgb(r g b/.14)` strings for
  `--line`, `--line-soft` and `--selected`, and a CSS font list for the optional
  `--ui-font`. It is `null` when "Match vault appearance" is off or the snapshots
  give no readable palette. While it is set, the whole app, its web views included,
  takes `mode`, as the Mac's window does. It is in the mode the Mac's Color theme
  picks while the look is on (`vault_mode`: Same as Obsidian, System, Light or Dark).
- `vaults` (optional, added 2026-10-02) is the same palette for each of the vault's
  colour modes the Obsidian plugin has measured: it measures the mode Obsidian shows
  and the other one beside it. A mode not measured yet is `null`, and `vaults` is
  `null` whenever `vault` is off. The phone's own Appearance setting (Same as Mac,
  System, Light or Dark) wears `vaults[mode]`. Notes are published with both modes'
  reading styles, each under its `prefers-color-scheme` block, once both are measured,
  so a note follows the mode the phone holds its web views to.
- `follow_page` is the Mac's "follow the page" setting (`html_follow_page`). While
  it is on, an HTML page's measured colours (the page's opaque body ground, or the
  root's when the body is transparent; none when either has a background image; the
  body's text colour; the first visible link's colour, or Onyx blue
  `rgb(58, 131, 247)` when there is none) go through the same palette rules as
  `POST /api/page-look`. The chrome wears that palette while the page is showing.
  `tests/fixtures/mirror_look_vectors.json` holds the cases a port must reproduce.
- `appearance` is the Mac's app theme (`system`, `light` or `dark`), used when
  `vault` is `null`.

**Library (optional, added 2026-09-30).** A second small object, named `library`
(`id("library")`, sealed and raw-DEFLATEd like the index), carries what the Mac's
Library home shows. It is its own object because it changes whenever a page is
opened, and re-sending the 1.7 MB index for that would be waste:

```json
{
  "v": 1,
  "recent": [{"id": "<page id>", "opened_at": 1790000000.0}],
  "chats": [{"id": "<chat page id>", "doc": "<page id>", "title": "…",
             "action": "ask", "turns": 3, "started_at": 1790000000.0,
             "updated_at": 1790000500.0}]
}
```

- `recent`: the Mac's recently opened documents (newest first, at most 30), only
  those that are published pages; a document outside the mirror is never listed.
- `chats`: conversation threads (a question and its follow-ups), newest first by
  `updated_at`, at most 100, only completed asks, and only when `mirror.toml` says
  `chats = true` (off by default) and only for a thread whose document is a
  published page. `action` is `ask`, `eli5` or `prove` (the thread's first
  turn); `turns` counts completed answers in it.
- Each thread is also a page in the index: `kind` `"chat"`, path
  `Chats/<first request id>`, name `page:Chats/<first request id>`. Its HTML is
  built on the Mac: a link back to its document (`href="<page id>"`), then each
  turn's label, the highlighted passage (first turn), the question, and the answer
  rendered from Markdown with raw HTML off, in the vault's reading theme. Links in
  an answer are kept only for `http(s)`; anything else becomes
  `#onyx-unpublished`. Chat pages are searchable on the phone but are not part of
  the folder tree.
- The phone may merge `recent` with its own record of pages it opened (it never
  writes back), newest first.
- Remove from Recents on the Mac drops the page from `recent` and the thread from
  `chats`. The phone has its own Remove from Recents, which hides a row on the
  phone only, since it never writes back. A row hidden there returns when its
  `opened_at` or `updated_at` moves past the time it showed when it was removed.
- The library is uploaded after the index, so everything it names is already
  there; the phone ignores any id the index doesn't list.

**Pages.** UTF-8 HTML, complete documents. A link to another published page is
the relative `href="<id>"` (plus `#fragment`); an image or other asset is
`src="<id>"`; a link to a page that isn't published is
`href="#onyx-unpublished"`. The phone serves every object at
`onyx-mirror://o/<id>`, so relative ids resolve with no scheme in the page.

**Pairing.** `onyxmirror1:` + base64url, no padding, of JSON
`{"u": "<Worker base URL, no trailing slash>", "t": "<read token>",
"k": "<base64url M, no padding>"}`. The Mac shows it as a QR code in the
terminal (`onyx mirror pair`) or copies it (`--copy`); the phone scans or pastes
it and keeps it in its Keychain.

**Mac-side config.** `~/Library/Application Support/Onyx/mirror.toml`:

```toml
enabled = true
interval_minutes = 5
include = ["Notes/Areas", "Artifacts"]   # mirror paths; "Notes" is the whole vault
exclude = []
```

Keychain service `onyx-mirror`, one item per account: `master_key`,
`read_token`, `worker_url`, `r2_account_id`, `r2_bucket`, `r2_access_key_id`,
`r2_secret_access_key`.

## Pieces

| Piece | Where | Public? |
|---|---|---|
| Publisher, crypto, uploader | `src/onyx/mirror/` | Yes (generic, inert by default) |
| Worker | `integrations/mirror-worker/` (+ `wrangler.example.toml`) | Yes (no config) |
| iOS app | its own private repo (decided) | No |

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
  longer lists. It runs every 5 minutes while enabled, plus a *Publish now*.
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

## Decisions

- The iOS app stays in its own private repo (decided 2026-09-30).
- Publish every 5 minutes while enabled, plus *Publish now* (decided 2026-09-30).
- Still open: which folders go in the include list. Client folders are the ones
  to decide on deliberately; encryption makes including them safe, but it's
  still a copy on a phone. This lives only in `mirror.toml`, never in the repo.

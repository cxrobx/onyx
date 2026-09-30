# Development and verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.lock
PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests -v

.venv/bin/python -m pip install -r requirements-browser.lock
.venv/bin/python -m playwright install chromium webkit
PYTHONPATH=src .venv/bin/python -m unittest tests.browser_smoke -v

swiftc -typecheck -F "$(./launcher/fetch-sparkle.sh)" -framework Cocoa -framework WebKit \
  -framework UniformTypeIdentifiers -framework Sparkle launcher/Onyx.swift
plutil -lint launcher/Info.plist

cd editor && npm ci && npm run check && npm run build && cd ..   # ⌘E editor → static/onyx-editor.js

./launcher/build-app.sh --no-install
./scripts/smoke-bundle.sh
```

CI runs the Python suite on Python 3.11 and 3.14, type-checks the Swift launcher,
validates the plist, builds the frozen service, and smoke-tests its health and
configuration contracts. It also rebuilds the editor and fails if the result
differs from the committed `static/onyx-editor.js`.

For a distributable release, run the release script. It needs a Developer ID
identity (`ONYX_SIGN_IDENTITY`, or the only one in your keychain), a notarytool
keychain profile, and the Sparkle signing key (below):

```bash
ONYX_NOTARY_PROFILE="notary-profile" scripts/release.sh 0.6.4
```

It refuses a dirty git tree, an existing tag `v0.6.4`, and metadata that
disagrees (`pyproject.toml`, `onyx.__version__` and `Info.plist` must all say
`0.6.4`, and `release-notes/v0.6.4.md` must exist). Then it builds through
`launcher/build-app.sh`, which notarizes the ZIP, staples the app, then creates
and notarizes a DMG with an Applications shortcut, staples it, and writes a
SHA-256 file for each download. It signs the ZIP for Sparkle, writes
`appcast.xml` (the release notes are its description), checks the result with
`scripts/verify-update-feed.sh`, and leaves everything in `dist/v0.6.4/`. Last,
it **prints** the `gh release create` command; it runs that only with
`--publish`. (`--allow-dirty` makes a dry run from a tree with uncommitted work,
which can never be published.) The Alfred workflow's name carries no version, so
`releases/latest/download/Open-in-Onyx.alfredworkflow` always serves the newest
copy, and the README links there. The frozen Python service uses the build machine's architecture;
the arm64 release requires an Apple Silicon Mac.

Without those variables, a local build signs with your keychain's Apple
Development identity when you have one. macOS then keeps the app's Documents
and Google Drive permissions across rebuilds. With no such identity (as in CI),
or with `ONYX_SIGN_IDENTITY=-`, it signs ad hoc. An ad-hoc signature changes
with every build, so macOS asks for those permissions again after each install,
and the vault sidebar waits until they are answered.

## Updates

Onyx updates itself with [Sparkle](https://sparkle-project.org) 2. `launcher/fetch-sparkle.sh`
downloads the pinned release, checks its sha256 and caches it (the framework is never
committed); `build-app.sh` links it and embeds it in `Contents/Frameworks`, signing its
helpers inside out as Sparkle documents for Developer ID.

- **The feed** is `https://github.com/cxrobx/onyx/releases/latest/download/appcast.xml`
  (`SUFeedURL` in `launcher/Info.plist`): every release carries its own `appcast.xml`, and
  GitHub serves the newest release's. The app checks daily and from **Onyx ▸ Check for
  Updates…**, prompts before installing (`SUAutomaticallyUpdate` is off), and verifies the
  download's EdDSA signature before it extracts it (`SUVerifyUpdateBeforeExtraction`).
- **The key.** `SUPublicEDKey` is the public half. The private half is in the login Keychain
  as Sparkle account `onyx` (`launcher/fetch-sparkle.sh` prints where Sparkle's `bin/` is;
  `generate_keys --account onyx`), with a backup in the secrets store as
  `SPARKLE_ED_PRIVATE_ONYX`. Lose both and no installed copy can ever update: the key is the
  identity of the update channel. Release with
  `secret run -k SPARKLE_ED_PRIVATE_ONYX -- scripts/release.sh X.Y.Z`: the script takes the key
  out of the environment before anything else runs (so the build and notarytool never see it)
  and hands it to `sign_update` on stdin. Reading it from the Keychain instead can stop to ask
  for the login password. `scripts/release.sh X.Y.Z --check` runs the checks before the build,
  including a probe signature against `SUPublicEDKey`. To back the key up again: export it with
  `generate_keys --account onyx -x <mode-0600 file>`, then `secret set SPARKLE_ED_PRIVATE_ONYX < file`,
  then `rm -P` the file (`-x /dev/stdout` fails and leaves its error message as the "key").
  Never commit it.
- **Checking a feed.** `scripts/verify-update-feed.sh` fetches the production feed (or a
  local `dist/vX.Y.Z/appcast.xml`), downloads each enclosure and checks its length, its
  signature against the public key in `Info.plist`, and the app inside (version, key, code
  signature, notarization ticket). Run it after publishing.
- **The background service.** An update replaces the app, not a service
  (`scripts/install-daemon.sh`) already running on the old code, so a launch that finds a
  service reporting a different version than the app's restarts the LaunchAgent. Only the
  copy in `/Applications` does, since that is the app `scripts/onyx-daemon.sh` runs.
- **The custom icon.** `adoptCustomIcon` writes `Icon\r` into the installed bundle, so
  `codesign --verify` fails on an installed Onyx by design. Sparkle still installs over it:
  tested with a signed build that had run long enough to set the icon (that copy failed
  `codesign --verify`; the copy that replaced it passed, and set its own icon on first launch).

Testing an update end to end means building two signed versions, pointing the older
one's `SUFeedURL` at a local `python3 -m http.server` (a test build only: a release build
keeps the https URL, and a unit test holds it to that), and installing the newer one from
the menu. A zip signed with another key, or with one byte changed, must be refused. (Clicking
Sparkle's Install button needs Accessibility access for whatever drives it. Without that, set
`defaults write <test bundle id> SUAutomaticallyUpdate -bool true` on the test build: it then
downloads and verifies on its own and installs when the app quits.)

## Project layout

```text
onyx/
├── editor/                   the ⌘E Live Preview editor (CodeMirror 6, esbuild → static/onyx-editor.js)
├── integrations/
│   ├── alfred/               Alfred workflow: onx/onxc search, file action
│   └── obsidian/             Obsidian plugin (TypeScript, esbuild)
├── launcher/                 native Swift app and release build
├── scripts/                  smoke tests, background daemon, plugin install
├── src/onyx/
│   ├── app.py                HTTP API, capabilities, persistence orchestration
│   ├── claude_runner.py      Claude process lifecycle and SSE translation
│   ├── codex_runner.py       Codex headless JSONL lifecycle and SSE translation
│   ├── citations.py          evidence validation and source opening
│   ├── diagnostics.py        provider/runtime/database diagnostics
│   ├── launcher_ui.py        shared glass, theme tokens, and the sidebar grid
│   ├── panels_ui.py          Settings and Recent conversations dialogs
│   ├── providers.py          subscription auth and live model discovery
│   ├── runner.py             subscription-only provider dispatch
│   ├── storage.py            SQLite schema and queries
│   ├── vault.py              vault index (notes + HTML): tree, wikilinks, titles, links
│   ├── vault_ui.py           the app's shell: Library · Notes · Artifacts beside the reader
│   └── viewer.py             secure HTML/Markdown/text/PDF readers
├── static/ask.js             selection UI and streamed answer panel
├── static/onyx-editor.js     the editor's build, committed; loaded on the first ⌘E
├── tests/                    API, security, storage, viewer, and runner tests
└── requirements-*.lock       exact runtime, build, and test environments
```

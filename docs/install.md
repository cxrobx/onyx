# Install, build, and run

The [README quickstart](../README.md#quickstart) is the short path. This page has the rest.

## Build the macOS app from source

Requirements for building: macOS 13 or newer, Xcode command-line tools, and
Python 3.11 or newer, and Node.js for the Obsidian plugin the app carries. The built app needs at least one supported CLI installed:
Claude Code signed into claude.ai, or Codex signed in with ChatGPT.

```bash
git clone https://github.com/cxrobx/onyx.git
cd onyx
./launcher/build-app.sh
open -a "Onyx"
```

The build installs `/Applications/Onyx.app`, refreshes macOS Services, and
also produces:

```text
launcher/build/Onyx-0.6.4-macOS-arm64.zip
launcher/build/Onyx-0.6.4-macOS-arm64.zip.sha256
launcher/build/Onyx-0.6.4-macOS-arm64.dmg
launcher/build/Onyx-0.6.4-macOS-arm64.dmg.sha256
launcher/build/Open-in-Onyx.alfredworkflow
launcher/build/Open-in-Onyx.alfredworkflow.sha256
```

Use `./launcher/build-app.sh --no-install` to build without replacing the
installed app. The prior app is staged as a backup during installation and is
restored if the replacement fails.

The launcher:

- accepts only an Onyx service with the expected service identity and
  protocol version;
- starts the bundled service when no compatible service is running;
- stops its owned service on normal quit and uses a parent-process watcher for
  crash/force-quit cleanup;
- waits up to 20 seconds for a healthy service and offers Retry, Open Log, and
  Quit on failure;
- writes service output to
  `~/Library/Logs/Onyx/onyx.log` and rotates it at 2 MB;
- discovers Claude and Codex through the login shell and common GUI-safe paths,
  including NVM-installed Codex binaries;
- updates itself with [Sparkle](https://sparkle-project.org): it checks the release feed daily and on
  **Onyx ▸ Check for Updates…**, asks before installing, and refuses a download whose EdDSA signature
  is not ours (see [Updates](development.md#updates));
- restarts the background service (`scripts/install-daemon.sh`) when its version is not the app's.

## Run from a checkout

`run.sh` creates or repairs a project-local virtual environment using the exact
versions in `requirements-runtime.lock`.

```bash
./run.sh --folder ~/Projects/my-project --port 8899
open http://127.0.0.1:8899/
```

Set `ONYX_VENV` to keep the runtime environment elsewhere. Source code is
loaded directly from `src/`, so normal edits do not trigger a reinstall.

## Run headless

The service normally starts with the app, but it can also run on its own so the
Obsidian plugin and any browser page work with nothing open:

```bash
./scripts/install-daemon.sh              # LaunchAgent, starts at login
./scripts/install-daemon.sh --status
./scripts/install-daemon.sh --uninstall
```

The agent runs the same bundled server the app would spawn, logging to
`~/Library/Logs/onyx-daemon.log`. The two coexist: the launcher adopts a
healthy service rather than starting a second one, and only terminates a server
it spawned itself, so quitting the app leaves the daemon serving. If the app
happens to own the port, the daemon idles and takes over when the app quits.

## CLI options

| Flag | Default | Purpose |
|---|---|---|
| `--folder DIR` | `~/Projects` | Default context folder. |
| `--port N` | `8899` | Local service port. |
| `--host H` | `127.0.0.1` | Bind address; keep this loopback. |
| `--model M` | `sonnet` | Initial Claude model before saved settings override it. |
| `--allow-root DIR` | — | Add a trusted context root; repeatable. |
| `--allow-any` | off | Allow any readable directory. This disables the root boundary. |
| `--data-dir DIR` | app support | Override database location for development/tests. |

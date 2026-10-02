"""FastAPI app: serves the widget and the SSE /ask endpoint.

Security model (all enforced on POST /ask):
  1. Server binds 127.0.0.1 only (see __main__.py).
  2. Host header must be 127.0.0.1:<port> or localhost:<port> — blocks
     DNS-rebinding, where a malicious page resolves its own domain to 127.0.0.1.
  3. Origin allowlist (NOT ``*``): null (file://), http(s)://localhost[:*],
     http(s)://127.0.0.1[:*], plus any origin the user added to the
     ``allowed_origins`` setting (``app://obsidian.md`` by default, for the
     Obsidian plugin). ``*`` stays only on GET /ask.js so the script tag
     loads from any page.
  4. Per-server random token, baked into ask.js at serve time, required in the
     /ask body.
  5. Folder allowlist via AppConfig.resolve_allowed (resolve() + is_relative_to).
  6. Read-only tool locks live in the Claude and Codex runner commands.

Every refusal is an ``event: error`` on a 200 SSE stream (not 403/429) so the
widget's stream reader can parse and display it.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import secrets
import sys
import time
import urllib.parse
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .runner import _sse, stream_answer
from .citations import open_source, reader_target
from .config import AppConfig
from .diagnostics import build_diagnostics
from .prompts import append_system_for, build_handoff_prompt, build_user_prompt
from .providers import find_claude, find_codex, provider_catalogs, provider_status
from .storage import Storage
from .vault_ui import vault_page
from . import first_run, handoff, markdown_theme, search, sidebar_theme, vault, vault_look, vault_mode, viewer
from . import __version__

logger = logging.getLogger("onyx.app")

_FROZEN_ROOT = getattr(sys, "_MEIPASS", None)
STATIC_DIR = (
    Path(_FROZEN_ROOT) / "static"
    if _FROZEN_ROOT
    else Path(__file__).resolve().parent.parent.parent / "static"
)
ASK_JS = STATIC_DIR / "ask.js"
MARK_PNG = STATIC_DIR / "onyx-mark.png"
APP_MENU_JS = STATIC_DIR / "app-menu.js"
EDITOR_JS = STATIC_DIR / "onyx-editor.js"
TOKEN_PLACEHOLDER = "__ASK_TOKEN__"
PROTOCOL_VERSION = 3

MAX_CONCURRENT = 3
MAX_SELECTION = 4000
MAX_CONTEXT = 4000
MAX_RECENT = 8
# Follow-up conversation history sent back by the widget. Bound both the number
# of turns and each turn's length so a malicious page can't blow up the prompt.
MAX_HISTORY_TURNS = 12
MAX_HISTORY_TEXT = 4000
MAX_ASSET_CAPABILITIES = 32
ASSET_CAPABILITY_TTL = 8 * 60 * 60
MAX_EDIT_CAPABILITIES = 64


def _sanitize_history(raw: object) -> list[dict]:
    """Coerce the request's ``history`` into a bounded list of clean turns."""
    if not isinstance(raw, list):
        return []
    turns: list[dict] = []
    for item in raw[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") == "assistant" else "user"
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()[:MAX_HISTORY_TEXT]
        if text:
            turns.append({"role": role, "text": text})
    return turns

_LOCALHOST_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _origin_allowed(origin: str | None, extra: tuple[str, ...] | list[str] = ()) -> bool:
    if origin is None:
        return True  # no Origin header (same-origin / non-browser): nothing to block
    if origin == "null":
        return True  # file://
    if origin in extra:
        return True  # explicitly allowed in Settings (e.g. the Obsidian plugin)
    return bool(_LOCALHOST_ORIGIN.match(origin))


def _host_allowed(host: str, port: int) -> bool:
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def _cors_headers(origin: str | None, extra: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """CORS headers to echo for an allowed cross-origin request (empty otherwise)."""
    if origin is None or not _origin_allowed(origin, extra):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _remember_folder(app: FastAPI, folder: str) -> None:
    recent: list[str] = app.state.recent_folders
    if folder in recent:
        recent.remove(folder)
    recent.insert(0, folder)
    del recent[MAX_RECENT:]


def _runtime_roots(app: FastAPI) -> tuple[Path, ...]:
    roots: list[Path] = []
    for item in app.state.storage.roots():
        try:
            path = Path(item["path"]).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return tuple(roots)


def _resolve_folder(app: FastAPI, raw: str | None) -> Path | None:
    config: AppConfig = app.state.config
    value = raw or str(config.default_folder)
    if config.allow_any:
        try:
            path = Path(value).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_dir() else None
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not path.is_dir():
        return None
    for root in _runtime_roots(app):
        try:
            if path == root or path.is_relative_to(root):
                return path
        except ValueError:
            continue
    return None


def _vault_root(app: FastAPI, kind: str = "notes") -> Path | None:
    """A configured vault folder (lexical, normalized) or None when unset/missing.

    ``kind`` is "notes" (the Obsidian vault) or "html" (Artifacts).
    """
    config: AppConfig = app.state.config
    key = "html_vault_root" if kind == "html" else "vault_root"
    raw = str(app.state.storage.settings(model_default=config.model).get(key) or "").strip()
    if not raw:
        return None
    root = vault.normalize(Path(raw).expanduser())
    try:
        return root if root.is_absolute() and root.is_dir() else None
    except OSError:
        return None


def _vault_missing_error(app: FastAPI, kind: str) -> str:
    if kind != "html":
        return "No vault folder is configured."
    config: AppConfig = app.state.config
    raw = str(app.state.storage.settings(model_default=config.model).get("html_vault_root") or "").strip()
    if not raw:
        return "No Artifacts folder is configured."
    return f"The Artifacts folder does not exist yet: {raw}"


def _search_places(app: FastAPI):
    """``search.place`` over the two trees as they stand: vault-mcp's path → the row the sidebar lists it as."""
    trees = {kind: app.state.vault.get(root, kind) for kind in ("notes", "html") if (root := _vault_root(app, kind))}
    return lambda path: search.place(path, trees.get("notes"), trees.get("html"))


def _search_locate(app: FastAPI, path: str, kind: str) -> str | None:
    """``search.locate``: the page the reader is showing → vault-mcp's name for it, or None if no tree lists it.

    Both trees are asked, because one file can be in both — an Artifacts page is usually a link, and a link into the
    Notes vault makes the same bytes a note as well — and because a page opened from the Library before its trees
    arrive is named by whichever vault the shell is showing. Which of its names the index holds is the index's to say
    (``knows``): vault-mcp leaves out a vault HTML file that renders a same-name ``.md``, so the Notes name of such a
    page finds nothing while its ``Artifacts/`` one finds everything. ``kind`` only breaks a tie.
    """
    names = []
    for one in (kind, "html" if kind == "notes" else "notes"):
        root = _vault_root(app, one)
        found = search.locate(path, one, app.state.vault.get(root, one)) if root else None
        if found is not None and found not in names:
            names.append(found)
    return next((name for name in names if app.state.passages.knows(name)), names[0] if names else None)


def _register_context_root(app: FastAPI, folder: Path) -> Path | None:
    """Allow ``folder`` as a context root — unless it is the whole disk or home.

    Linking a page into Artifacts is the user saying "I want to read this
    with Ask", and its folder is where the evidence lives, so it joins the
    allowed roots (visible and removable in Settings). Home and ``/`` would
    quietly open everything, so a link from there keeps its default context.
    """
    try:
        resolved = folder.resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir() or resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        return None
    app.state.storage.add_root(resolved)
    return resolved


def _register_vault_root(app: FastAPI) -> None:
    # The vault is browsed through /view with folder=<vault>, so it must also be
    # an allowed context root or the reader's folder seed silently falls back.
    root = _vault_root(app)
    if root is not None:
        app.state.storage.add_root(root)


def _decode_sse(chunk: str) -> tuple[str | None, dict]:
    event = None
    payload: dict = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                decoded = json.loads(line[6:])
                payload = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                payload = {}
    return event, payload


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"\s*<!doctype[^>]*>", re.IGNORECASE)


def _error_page(message: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Onyx — can't open</title>"
        "<body style='font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:640px;"
        "margin:80px auto;padding:0 24px;color:#1c1917'>"
        "<h1 style='color:#c2410c'>Couldn't open that document</h1>"
        f"<p>{_esc(message)}</p>"
        # It usually shows in the shell's reader frame: _top takes the window back to Library, not the frame.
        "<p><a href='/' target='_top' style='color:#c2410c'>&larr; back to Library</a></p>"
    )


def create_app(config: AppConfig) -> FastAPI:
    storage = Storage(config.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The phone mirror's publisher. It starts only when the real entry point asked (config.mirror) and
        # mirror.toml says enabled = true; otherwise no thread, no file, no network (mirror/service.py).
        publisher = None
        if config.mirror:
            from .mirror import service as mirror_service

            publisher = mirror_service.start(
                storage,
                lambda: mirror_service.roots_from_settings(storage.settings(model_default=config.model)),
            )
        yield
        if publisher is not None:
            publisher.stop()
        storage.close()

    app = FastAPI(title="Onyx", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.storage = storage
    app.state.storage.sync_builtin_roots(config.allowed_roots)
    app.state.vault = vault.VaultCache(ttl=5.0)
    # ⌘P's passages: vault-mcp's index, read where it lies (search.py).
    app.state.passages = search.PassageIndex(search.index_path(), ollama=search.ollama_url())
    if config.first_run:
        first_run.adopt_folders(storage, model_default=config.model)
    _register_vault_root(app)
    app.state.sem = asyncio.Semaphore(MAX_CONCURRENT)
    app.state.recent_folders = [str(config.default_folder)]
    # Per-document expiring capabilities replace the old process-wide asset set.
    app.state.asset_caps = OrderedDict()
    # The editor's: one per note opened for editing (/api/source), naming the one file it may save. Kept apart from
    # the asset capabilities, which expire and are pushed out by the next 32 pages opened, under a note being written.
    app.state.edit_caps = OrderedDict()
    # Each editor's images come through /_fs under a capability of its own (the edit capability's "fs"), which lists
    # only images the saved note references (/api/source/images).
    app.state.edit_fs = {}

    def allowed_origins() -> list[str]:
        raw = storage.settings(model_default=config.model).get("allowed_origins")
        return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []

    def origin_ok(origin: str | None) -> bool:
        return _origin_allowed(origin, allowed_origins())

    def cors(origin: str | None) -> dict[str, str]:
        return _cors_headers(origin, allowed_origins())

    def err_stream(message: str, origin: str | None) -> StreamingResponse:
        async def gen():
            yield _sse("error", {"message": message})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={**cors(origin), **_SSE_HEADERS},
        )

    @app.get("/health")
    async def health(request: Request):
        # The native launcher validates this identity instead of assuming that
        # any process returning HTTP 200 on port 8899 is safe to embed.
        return JSONResponse(
            {
                "status": "ok",
                "service": "onyx",
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}",
                "claude_available": find_claude() is not None,
                "codex_available": find_codex() is not None,
            },
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/ask.js")
    async def ask_js():
        try:
            text = ASK_JS.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Response("// ask.js missing", status_code=500, media_type="application/javascript")
        text = text.replace(TOKEN_PLACEHOLDER, config.token)
        return Response(
            content=text,
            media_type="application/javascript",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )

    @app.get("/onyx-mark.png")
    async def onyx_mark():
        # The sidebar logo on the launcher and vault pages.
        try:
            return Response(
                content=MARK_PNG.read_bytes(),
                media_type="image/png",
                headers={"Cache-Control": "max-age=86400"},
            )
        except FileNotFoundError:
            return Response(status_code=404)

    @app.get("/app-menu.js")
    async def app_menu_js():
        # The rendered right-click menu the app's own pages share.
        try:
            text = APP_MENU_JS.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Response("// app-menu.js missing", status_code=500, media_type="application/javascript")
        return Response(content=text, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/config")
    async def get_config(request: Request):
        origin = request.headers.get("origin")
        settings = app.state.storage.settings(model_default=config.model)
        body = {
            "default_folder": str(config.default_folder),
            "allowed_roots": ["(any)"] if config.allow_any else [str(r) for r in _runtime_roots(app)],
            "recent_folders": list(app.state.recent_folders),
            "model": settings["model"],
            "provider": settings["provider"],
            "reasoning_effort": settings["reasoning_effort"],
            "version": __version__,
            "cache_ttl_hours": settings["cache_ttl_hours"],
            "cache_max_entries": settings["cache_max_entries"],
            "appearance_theme": settings["appearance_theme"],
        }
        return JSONResponse(body, headers=cors(origin))

    @app.get("/quick", response_class=HTMLResponse)
    async def quick_read(request: Request, text: str, folder: str | None = None):
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return HTMLResponse(_error_page("Refused: host not allowed."), status_code=403)
        passage = text.strip()[:20_000]
        if not passage:
            return HTMLResponse(_error_page("No selected text was provided."), status_code=400)
        origin = str(request.base_url).rstrip("/")
        seed_folder = _resolve_folder(app, folder) if folder else config.default_folder
        if seed_folder is None:
            return HTMLResponse(_error_page("The saved context folder is no longer allowed."), status_code=400)
        source = f"service://selection/{uuid.uuid4().hex[:12]}"
        body = (
            "<h1>Shared selection</h1><p>Select the passage or right-click it to ask.</p>"
            f'<blockquote id="askw-quick-selection">{_esc(passage)}</blockquote>'
        )
        html_text = viewer._reading_shell("Shared selection", body, kind="selection")
        if css := current_markdown_theme()["css"]:
            html_text = html_text.replace("</head>", f'<style id="askw-markdown-theme">{css}</style></head>', 1)
        seed = (
            f'<meta name="askw-folder" content="{_esc(str(seed_folder))}">'
            f'<meta name="askw-src" content="{_esc(source)}">'
            '<meta name="askw-auto-selection" content="1">'
            f'<script src="{origin}/ask.js"></script>'
        )
        html_text = html_text.replace("</body>", seed + "</body>")
        app.state.storage.upsert_document(
            source=source,
            title="Shared selection",
            kind="selection",
            folder=str(seed_folder),
        )
        return HTMLResponse(
            html_text,
            headers={
                "Content-Security-Policy": "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'"
            },
        )

    @app.get("/view", response_class=HTMLResponse)
    async def view(request: Request, src: str, folder: str | None = None):
        # Host check (also makes request.base_url safe to interpolate, and blocks
        # DNS-rebinding to this route).
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return HTMLResponse(_error_page("Refused: host not allowed."), status_code=403)
        # Same-origin as the page the user navigated to (localhost vs 127.0.0.1
        # must match, or the injected ask.js would be cross-origin).
        origin = str(request.base_url).rstrip("/")
        settings = app.state.storage.settings(model_default=config.model)
        seed_path = _resolve_folder(app, folder) if folder else config.default_folder
        seed = str(seed_path) if seed_path else None
        editing = None  # how a note was opened, which its links resolve against once it is being edited
        shown = None    # the version of the file this page shows, which a task tick is made against
        try:
            if viewer.is_remote(src):
                html_text = await asyncio.to_thread(
                    viewer.fetch_remote,
                    src,
                    allow_private=bool(settings["allow_private_remote"]),
                )
                doc_src = src
                title_match = re.search(
                    r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL
                )
                title = (
                    html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
                    if title_match
                    else src
                )
                kind = "remote-html"
                page_count = None
            else:
                raw = src.strip()
                if raw.lower().startswith("file://"):
                    # file:///Users/...  or  file://localhost/Users/...  → /Users/...
                    raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
                candidate = Path(raw).expanduser()
                if not candidate.is_absolute():
                    return HTMLResponse(
                        _error_page(
                            "Please paste an absolute path (starting with /) or a file:// URL — "
                            "or use “Choose file…”."
                        ),
                        status_code=400,
                    )
                # ``lexical`` is the path as the user sees it (a note under a
                # symlinked vault folder stays vault-visible); ``path`` is the
                # realpath used for history, positions, and live reload.
                lexical = vault.normalize(candidate)
                path = candidate.resolve()
                html_root = _vault_root(app, "html")
                if not folder and html_root is not None and vault.is_inside(lexical, html_root):
                    # A page in Artifacts reads with the real folder behind its
                    # link as context; the vault itself holds only symlinks.
                    context = vault.html_context_folder(lexical, html_root)
                    context_path = _resolve_folder(app, str(context)) if context else None
                    if context_path is not None:
                        seed = str(context_path)
                root = _vault_root(app)
                # Taken before the file is read: should it change in between, the page shows newer text than this
                # names, and a tick made against it is refused rather than landing on a line the page never showed.
                try:
                    shown = viewer.stat_signature(path.stat())
                except OSError:
                    shown = None
                index = None
                if root is not None and vault.is_inside(lexical, root):
                    index = await asyncio.to_thread(app.state.vault.get, root)
                loaded = await asyncio.to_thread(
                    viewer.load_local_document,
                    path,
                    display_path=lexical,
                    folder=seed,
                    vault=index,
                )
                html_text = loaded.html
                if loaded.kind == "markdown":
                    editing = {"display": str(lexical), "folder": seed, "notes": str(root) if index is not None else None}
                if loaded.kind in markdown_theme.KINDS:
                    css = current_markdown_theme()["css"]
                    html_text = html_text.replace(
                        "</head>", f'<style id="askw-markdown-theme">{css}</style></head>', 1
                    )
                doc_src = str(path)
                title = loaded.title
                kind = loaded.kind
                page_count = loaded.page_count
        except viewer.ViewerError as exc:
            return HTMLResponse(_error_page(str(exc)), status_code=400)
        capability = secrets.token_urlsafe(18)
        interactive_local_html = kind == "html" and not viewer.is_remote(doc_src)
        out, assets = viewer.prepare_html(
            doc_src,
            html_text=html_text,
            server_origin=origin,
            folder=seed,
            asset_token=capability,
            allow_document_scripts=interactive_local_html,
        )
        out = first_paint(out, doc_src, settings, kind=kind, shown=shown if kind == "markdown" else None)
        caps: OrderedDict = app.state.asset_caps
        caps[capability] = {
            "assets": assets,
            "source": doc_src,
            "expires": time.time() + ASSET_CAPABILITY_TTL,
            **({"editing": editing} if editing else {}),
        }
        caps.move_to_end(capability)
        while len(caps) > MAX_ASSET_CAPABILITIES:
            caps.popitem(last=False)
        app.state.storage.upsert_document(
            source=doc_src,
            title=title,
            kind=kind,
            folder=seed,
            page_count=page_count,
        )
        # Trusted local HTML retains its authored scripts and inline button handlers.
        # Remote HTML remains inert. In both cases, scripts cannot connect away from
        # the local service, embed plugins, or change the document base URL.
        script_src = (
            "script-src 'self' 'unsafe-inline'"
            if interactive_local_html
            else "script-src 'self'"
        )
        csp = f"{script_src}; connect-src 'self'; object-src 'none'; base-uri 'none'"
        return HTMLResponse(out, headers={"Content-Security-Policy": csp})

    def _tag_vaults(items: list[dict], key: str) -> None:
        """Say which vault each reading-history row lives in, and where.

        History keys a document by its realpath, but the shell lists it under
        its vault path: a page in Artifacts by its link, a note under a linked
        folder by the link's side. ``vault_path`` is that row, so the reader
        opens it there and the sidebar can highlight it; ``vault_folder`` is
        the folder it sits in, for saying where it lives.
        """
        indexes = [
            (kind, app.state.vault.get(root, kind))
            for kind in ("notes", "html")
            if (root := _vault_root(app, kind)) is not None
        ]
        for item in items:
            item["vault"] = item["vault_path"] = item["vault_folder"] = None
            source = str(item.get(key) or "")
            if not source.startswith("/"):
                continue
            for kind, index in indexes:
                row = index.by_real(source)
                if row is not None:
                    item["vault"], item["vault_path"] = kind, str(row.path)
                    item["vault_folder"] = row.entry_rel.rpartition("/")[0] if kind == "html" else row.folder
                    break

    async def _shell(
        request: Request,
        kind: str,
        *,
        src: str | None,
        folder: str | None,
        history: str | None,
        history_action: str | None,
    ) -> HTMLResponse:
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return HTMLResponse(_error_page("Refused: host not allowed."), status_code=403)
        settings = app.state.storage.settings(model_default=config.model)
        params: dict[str, str] | None = None
        if src and kind == "library":
            # Library reads anything. A page that lives in a vault opens as its
            # row there (Finder hands over the real file), so it is highlighted
            # and reads with that vault's context; anything else keeps the
            # folder it came with.
            found: dict = {"source": src}
            if not viewer.is_remote(src):
                await asyncio.to_thread(_tag_vaults, [found], "source")
            notes_root = _vault_root(app)
            if found.get("vault") == "notes" and notes_root is not None:
                params = {"src": found["vault_path"], "folder": str(notes_root)}
            elif found.get("vault") == "html":
                params = {"src": found["vault_path"]}
            else:
                params = {"src": src, **({"folder": folder} if folder else {})}
        elif src and (root := _vault_root(app, kind)) is not None:
            # The Obsidian vault is its own context; a page in Artifacts gets the
            # real folder behind its link, which /view works out from the path.
            params = {"src": src} if kind == "html" else {"src": src, "folder": str(root)}
        reader_query = None
        if params is not None:
            if history:
                params["history"] = history
            if history_action:
                params["history_action"] = history_action
            reader_query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/")
        return HTMLResponse(
            vault_page(
                config,
                settings,
                src=params["src"] if params else None,
                reader_query=reader_query,
                kind=kind,
                sidebar=current_sidebar_theme(),
                look=current_vault_look(),
            )
        )

    # The app opens on Library; Notes and Artifacts are the same page, switched in place.
    @app.get("/", response_class=HTMLResponse)
    async def library_view(
        request: Request,
        src: str | None = None,
        folder: str | None = None,
        history: str | None = None,
        history_action: str | None = None,
    ):
        return await _shell(request, "library", src=src, folder=folder, history=history, history_action=history_action)

    @app.get("/vault", response_class=HTMLResponse)
    async def vault_view(
        request: Request,
        src: str | None = None,
        folder: str | None = None,
        history: str | None = None,
        history_action: str | None = None,
        vault_kind: str = Query("notes", alias="vault"),
    ):
        kind = vault_kind if vault_kind in {"html", "library"} else "notes"
        return await _shell(request, kind, src=src, folder=folder, history=history, history_action=history_action)

    @app.get("/_fs/{capability}/{path:path}")
    async def fs_asset(request: Request, capability: str, path: str):
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return Response("Forbidden", status_code=403)
        cap = app.state.asset_caps.get(capability)
        editing = app.state.edit_caps.get(app.state.edit_fs.get(capability, "")) if cap is None else None
        if editing is not None:
            allowed = editing["assets"]
        elif not cap or cap["expires"] < time.time():
            app.state.asset_caps.pop(capability, None)
            return Response("Expired", status_code=404)
        else:
            allowed = cap["assets"]
        result = viewer.resolve_fs_path(path, allowed=allowed, home=Path.home())
        if result is None:
            return Response("Not found", status_code=404)
        abspath, ctype = result
        headers = {"Cache-Control": "no-cache", "Accept-Ranges": "bytes"}
        try:
            size = (await asyncio.to_thread(abspath.stat)).st_size
            span = viewer.byte_range(request.headers.get("range"), size)
        except viewer.RangeNotSatisfiable:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        except OSError:
            return Response("Not found", status_code=404)
        if span is None:
            data = await asyncio.to_thread(abspath.read_bytes)
            return Response(content=data, media_type=ctype, headers=headers)
        start, end = span

        def read_span() -> bytes:
            with open(abspath, "rb") as handle:
                handle.seek(start)
                return handle.read(end - start + 1)

        data = await asyncio.to_thread(read_span)
        headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{size}"
        return Response(content=data, status_code=206, media_type=ctype, headers=headers)

    @app.get("/_mtime")
    async def doc_mtime(request: Request, src: str, cap: str):
        # Cheap change-detection for live reload: returns a stat signature the
        # /view page polls. Host-checked like /_fs; returns strictly less than
        # /view already does (which serves the file's full contents). Live reload
        # is local-only — remote URLs have no mtime.
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return Response("Forbidden", status_code=403)
        capability = app.state.asset_caps.get(cap)
        if (
            not capability
            or capability["expires"] < time.time()
            or capability["source"] != src
        ):
            return JSONResponse({"ok": False})
        if viewer.is_remote(src):
            return JSONResponse({"ok": False})
        raw = src.strip()
        if raw.lower().startswith("file://"):
            raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False})
        if path.suffix.lower() not in viewer.LOCAL_DOCUMENT_EXTENSIONS:
            return JSONResponse({"ok": False})
        try:
            st = await asyncio.to_thread(path.stat)
        except (OSError, ValueError):
            return JSONResponse({"ok": False})
        return JSONResponse(
            {"ok": True, "sig": viewer.stat_signature(st)},
            headers={"Cache-Control": "no-store"},
        )

    # MARK: - Editing a note (⌘E): the editor in static/onyx-editor.js, built from editor/

    @app.get("/onyx-editor.js")
    async def editor_js():
        try:
            text = EDITOR_JS.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Response("// onyx-editor.js missing", status_code=500, media_type="application/javascript")
        return Response(content=text, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/api/source")
    async def note_source(request: Request, src: str, cap: str):
        """A note's Markdown for the editor, for the page that shows it: the page's own capability names the file, so
        this reads nothing that page wasn't already given. It hands back an edit capability for that one file."""
        if denied := api_forbidden(request):
            return denied
        capability = app.state.asset_caps.get(cap)
        if not capability or capability["expires"] < time.time() or capability["source"] != src:
            return JSONResponse({"ok": False, "error": "Reload this page to edit it."}, status_code=403)
        opened = capability.get("editing")
        if not opened:
            return JSONResponse({"ok": False, "error": "Only Markdown notes can be edited."}, status_code=400)
        try:
            text, sig = await asyncio.to_thread(viewer.read_source, Path(src))
        except viewer.ViewerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        edits: OrderedDict = app.state.edit_caps
        edit, fs = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        edits[edit] = {"source": src, "fs": fs, "assets": set(), **opened}
        app.state.edit_fs[fs] = edit
        while len(edits) > MAX_EDIT_CAPABILITIES:
            _, gone = edits.popitem(last=False)
            app.state.edit_fs.pop(gone["fs"], None)
        return JSONResponse({"ok": True, "text": text, "sig": sig, "edit": edit}, headers={"Cache-Control": "no-store"})

    def editing(edit: str) -> dict | None:
        """The note an edit capability names, kept at the fresh end: one in use is never the one pushed out."""
        opened = app.state.edit_caps.get(edit)
        if opened is not None:
            app.state.edit_caps.move_to_end(edit)
        return opened

    @app.get("/api/source/current")
    async def note_source_now(request: Request, edit: str):
        """The note's text as it is on disk now, read through the editor's own capability. Taking in a version written
        elsewhere goes this way, so it neither mints capabilities nor depends on the page's, which expire."""
        if denied := api_forbidden(request):
            return denied
        opened = editing(edit)
        if not opened:
            return JSONResponse({"ok": False, "error": "Onyx restarted since this note was opened; reload it."}, status_code=403)
        try:
            text, sig = await asyncio.to_thread(viewer.read_source, Path(opened["source"]))
        except viewer.ViewerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "text": text, "sig": sig}, headers={"Cache-Control": "no-store"})

    @app.post("/api/source")
    async def save_note_source(request: Request):
        """Save the editor's text to the one file its edit capability names, over the version it was written against;
        409 with the version on disk when that has changed since."""
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        edit = editing(str(body.get("edit") or ""))
        if not edit:
            return JSONResponse({"ok": False, "error": "Onyx restarted since this note was opened; reload it to keep editing."}, status_code=403)
        text = body.get("text")
        if not isinstance(text, str):
            return JSONResponse({"ok": False, "error": "no text"}, status_code=400)
        try:
            sig = await asyncio.to_thread(viewer.write_source, Path(edit["source"]), text, base=str(body.get("base") or ""))
        except viewer.SourceConflict as exc:
            return JSONResponse({"ok": False, "error": str(exc), "sig": exc.sig}, status_code=409)
        except viewer.ViewerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "sig": sig}

    @app.post("/api/source/images")
    async def note_images(request: Request):
        """The URLs to draw the editor's images with. An image is served only when the note as saved on disk references
        it, as /view would serve it for this note: the reader renders the saved note for the list, and the editor asks
        again after its next save for one typed since."""
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        opened = editing(str(body.get("edit") or ""))
        if not opened:
            return JSONResponse({"ok": False, "error": "Reload this page to see its images."}, status_code=403)
        refs = [r for r in body.get("refs") or [] if isinstance(r, dict)][:200]

        def resolve() -> dict[str, str | None]:
            notes = opened.get("notes")
            index = app.state.vault.get(Path(notes)) if notes else None
            display = Path(opened["display"])
            loaded = viewer.load_local_document(
                Path(opened["source"]), display_path=display, folder=opened.get("folder"), vault=index
            )
            prefix = f"/_fs/{opened['fs']}"
            _, referenced = viewer.prepare_html(
                opened["source"], html_text=loaded.html, server_origin="", folder=opened.get("folder"), asset_token=opened["fs"]
            )
            ctx = viewer.RenderContext(doc_path=display, folder=opened.get("folder"), vault=index)
            urls: dict[str, str | None] = {}
            for ref in refs:
                kind, target = str(ref.get("kind") or "md"), str(ref.get("target") or "")
                found = viewer.image_asset(target, kind, ctx, src=opened["source"], fs_prefix=prefix)
                url = None
                if found is not None:
                    url, asset = found
                    if asset is not None:
                        if asset in referenced:
                            opened["assets"].add(asset)
                        else:
                            url = None
                urls[f"{kind}:{target}"] = url
            return urls

        try:
            urls = await asyncio.to_thread(resolve)
        except viewer.ViewerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "urls": urls}, headers={"Cache-Control": "no-store"})

    @app.post("/api/source/task")
    async def tick_task(request: Request):
        """A task box clicked on the reading page: its line ticked or cleared in the page's own note, over the version
        the page was showing (409 when the note has changed since, and the page reloads with it)."""
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        src = str(body.get("src") or "")
        capability = app.state.asset_caps.get(str(body.get("cap") or ""))
        if not capability or capability["expires"] < time.time() or capability["source"] != src or not capability.get("editing"):
            return JSONResponse({"ok": False, "error": "Reload this page to tick its tasks."}, status_code=403)
        try:
            line = int(body.get("line"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "no line"}, status_code=400)
        try:
            sig = await asyncio.to_thread(
                viewer.set_task, Path(src), line, bool(body.get("done")), base=str(body.get("base") or "")
            )
        except viewer.SourceConflict as exc:
            return JSONResponse({"ok": False, "error": str(exc), "sig": exc.sig}, status_code=409)
        except viewer.ViewerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "sig": sig}

    @app.get("/api/source/link")
    async def note_link(request: Request, edit: str, target: str, kind: str = "md"):
        """Where a link clicked in the editor leads, from where its note was opened (see ``viewer.link_href``)."""
        if denied := api_forbidden(request):
            return denied
        opened = editing(edit)
        if not opened:
            return JSONResponse({"ok": False, "error": "Reload this page to follow its links."}, status_code=403)

        def resolve() -> str | None:
            notes = opened.get("notes")
            index = app.state.vault.get(Path(notes)) if notes else None
            ctx = viewer.RenderContext(doc_path=Path(opened["display"]), folder=opened.get("folder"), vault=index)
            return viewer.link_href(target, kind, ctx)

        href = await asyncio.to_thread(resolve)
        return JSONResponse({"ok": href is not None, "href": href}, headers={"Cache-Control": "no-store"})

    # MARK: - Library, settings, and diagnostics APIs

    def api_forbidden(request: Request) -> JSONResponse | None:
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return JSONResponse({"ok": False, "error": "host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if not origin_ok(origin):
            return JSONResponse({"ok": False, "error": "origin not allowed"}, status_code=403)
        return None

    @app.get("/api/library")
    async def library(
        request: Request,
        q: str = "",
        provider: str = "",
        model: str = "",
        source: str = "",
        action: str = "",
        days: int = 0,
    ):
        if denied := api_forbidden(request):
            return denied
        storage: Storage = app.state.storage
        since = time.time() - min(max(days, 0), 3650) * 86_400 if days else None
        data = storage.search(
            q[:200],
            limit=100,
            provider=provider if provider in {"claude", "codex"} else None,
            model=model[:100] or None,
            source=source[:4000] or None,
            action=action if action in {"ask", "eli5", "prove"} else None,
            since=since,
        )
        # Library's home and Recent conversations open each row where the sidebar lists it.
        await asyncio.to_thread(_tag_vaults, data["documents"], "source")
        await asyncio.to_thread(_tag_vaults, data["conversations"], "document_source")
        return JSONResponse({"ok": True, **data}, headers=cors(request.headers.get("origin")))

    @app.post("/api/recent/remove")
    async def recent_remove(request: Request):
        """Remove from Recents: a page from Recently opened (by `source`) or a thread from Recent asks (by its latest
        `request_id`). Hidden, never deleted (Storage.hide_recent_document)."""
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        storage: Storage = app.state.storage
        source, request_id = body.get("source"), body.get("request_id")
        if isinstance(source, str) and 0 < len(source) <= 4000:
            found = await asyncio.to_thread(storage.hide_recent_document, source)
        elif isinstance(request_id, str) and 0 < len(request_id) <= 200:
            found = await asyncio.to_thread(storage.hide_recent_conversation, request_id)
        else:
            return JSONResponse({"ok": False, "error": "name a source or a request_id"}, status_code=400)
        if not found:
            return JSONResponse({"ok": False, "error": "not in recents"}, status_code=404)
        return JSONResponse({"ok": True}, headers=cors(request.headers.get("origin")))

    @app.get("/api/dock/recent")
    async def dock_recent(request: Request):
        if denied := api_forbidden(request):
            return denied
        # The Dock only needs a handful of viewed vault pages. Keep this separate
        # from Library search, which also returns conversation content.
        documents = await asyncio.to_thread(app.state.storage.recent_documents, 100)
        await asyncio.to_thread(_tag_vaults, documents, "source")
        items: dict[str, list[dict[str, str]]] = {"html": [], "notes": []}
        for document in documents:
            kind = document.get("vault")
            path = document.get("vault_path")
            if kind not in items or not path or len(items[kind]) >= 4:
                continue
            if not Path(path).is_file():
                continue
            items[kind].append({
                "title": str(document["title"]),
                "path": str(document["source"]),
                "folder": str(document.get("vault_folder") or ""),
            })
            if all(len(group) >= 4 for group in items.values()):
                break
        return JSONResponse({"ok": True, "artifacts": items["html"], "notes": items["notes"]},
                            headers={**cors(request.headers.get("origin")), "Cache-Control": "no-store"})

    @app.get("/api/history")
    async def history(request: Request, source: str, selection: str = "", limit: int = 20, page_only: bool = False):
        if denied := api_forbidden(request):
            return denied
        items = app.state.storage.recent_conversations(
            limit=limit, source=source[:4000],
            selection="" if page_only else selection[:MAX_SELECTION] or None,
        )
        return JSONResponse(
            {"ok": True, "conversations": items, "document": app.state.storage.document(source)},
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/api/conversations/{request_id}")
    async def conversation_api(request: Request, request_id: str):
        if denied := api_forbidden(request):
            return denied
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            return JSONResponse({"ok": False, "error": "invalid request id"}, status_code=400)
        item = app.state.storage.conversation(request_id)
        if item is None:
            return JSONResponse({"ok": False, "error": "history entry not found"}, status_code=404)
        return JSONResponse(
            {"ok": True, "conversation": item},
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/api/highlights")
    async def highlights_api(request: Request, source: str):
        if denied := api_forbidden(request):
            return denied
        if not source or len(source) > 4000:
            return JSONResponse({"ok": False, "error": "invalid source"}, status_code=400)
        return JSONResponse(
            {"ok": True, "highlights": app.state.storage.highlights(source)},
            headers=cors(request.headers.get("origin")),
        )

    @app.post("/api/highlights")
    async def add_highlight_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        source = body.get("document_source")
        selection = body.get("selection")
        if (not isinstance(source, str) or not 0 < len(source) <= 4000 or
                not isinstance(selection, str) or not selection.strip() or len(selection) > MAX_SELECTION):
            return JSONResponse({"ok": False, "error": "invalid highlight"}, status_code=400)
        context = body.get("context") if isinstance(body.get("context"), str) else ""
        prefix = body.get("prefix") if isinstance(body.get("prefix"), str) else ""
        suffix = body.get("suffix") if isinstance(body.get("suffix"), str) else ""
        page = body.get("document_page")
        if page is not None and (not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 100000):
            return JSONResponse({"ok": False, "error": "invalid page"}, status_code=400)
        item = app.state.storage.add_highlight(
            id=uuid.uuid4().hex, source=source, selection=selection.strip(),
            context=context[:MAX_CONTEXT], prefix=prefix[-64:], suffix=suffix[:64], page=page,
        )
        return JSONResponse({"ok": True, "highlight": item}, headers=cors(request.headers.get("origin")))

    @app.patch("/api/highlights/{highlight_id}")
    async def update_highlight_api(request: Request, highlight_id: str):
        if denied := api_forbidden(request):
            return denied
        if not re.fullmatch(r"[a-f0-9]{32}", highlight_id):
            return JSONResponse({"ok": False, "error": "invalid highlight id"}, status_code=400)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        note = body.get("note")
        if not isinstance(note, str) or len(note) > 2000:
            return JSONResponse({"ok": False, "error": "invalid note"}, status_code=400)
        item = app.state.storage.update_highlight_note(highlight_id, note.strip())
        if item is None:
            return JSONResponse({"ok": False, "error": "highlight not found"}, status_code=404)
        return JSONResponse({"ok": True, "highlight": item}, headers=cors(request.headers.get("origin")))

    @app.delete("/api/highlights/{highlight_id}")
    async def delete_highlight_api(request: Request, highlight_id: str):
        if denied := api_forbidden(request):
            return denied
        if not re.fullmatch(r"[a-f0-9]{32}", highlight_id):
            return JSONResponse({"ok": False, "error": "invalid highlight id"}, status_code=400)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        if not app.state.storage.delete_highlight(highlight_id):
            return JSONResponse({"ok": False, "error": "highlight not found"}, status_code=404)
        return JSONResponse({"ok": True}, headers=cors(request.headers.get("origin")))

    @app.get("/api/document")
    async def document_api(request: Request, source: str):
        if denied := api_forbidden(request):
            return denied
        return JSONResponse(
            {"ok": True, "document": app.state.storage.document(source[:4000])},
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/api/folder")
    async def validate_folder_api(request: Request, path: str):
        if denied := api_forbidden(request):
            return denied
        resolved = _resolve_folder(app, path)
        if resolved is None:
            return JSONResponse(
                {"ok": False, "error": "Folder is missing or outside the allowed roots."},
                status_code=400,
                headers=cors(request.headers.get("origin")),
            )
        return JSONResponse(
            {"ok": True, "path": str(resolved)},
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/api/vault/tree")
    async def vault_tree_api(request: Request, vault_kind: str = Query("notes", alias="vault")):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        kind = "html" if vault_kind == "html" else "notes"
        root = _vault_root(app, kind)
        if root is None:
            return JSONResponse(
                {"ok": False, "error": _vault_missing_error(app, kind)}, status_code=400, headers=headers
            )
        index = await asyncio.to_thread(app.state.vault.get, root, kind)
        return JSONResponse(
            {
                "ok": True,
                "root": str(root),
                "vault": kind,
                "built_at": index.built_at,
                "files": len([item for item in index.notes if not item.missing]),
                "missing": len([item for item in index.notes if item.missing]),
                "truncated": index.truncated,
                "tree": index.tree_json(),
            },
            headers=headers,
        )

    @app.get("/api/vault/search")
    async def vault_search_api(
        request: Request, q: str = "", limit: int = 50, vault_kind: str = Query("notes", alias="vault")
    ):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        kind = "html" if vault_kind == "html" else "notes"
        root = _vault_root(app, kind)
        if root is None:
            return JSONResponse(
                {"ok": False, "error": _vault_missing_error(app, kind)}, status_code=400, headers=headers
            )
        q = q[:200]
        limit = max(1, min(limit, 200))
        index = await asyncio.to_thread(app.state.vault.get, root, kind)
        items, truncated = index.search(q, limit=limit)
        return JSONResponse(
            {
                "ok": True,
                "q": q,
                "items": [
                    {
                        "name": item.name,
                        "title": item.label if kind == "html" else "",
                        "path": str(item.path),
                        "folder": item.entry_rel.rpartition("/")[0] if kind == "html" else item.folder,
                    }
                    for item in items
                ],
                "truncated": truncated,
            },
            headers=headers,
        )

    # ⌘P's passages (search.py, palette_ui.py): the pages whose words or meaning match, one row each, as the trees list
    # them. The index is vault-mcp's, read-only. The palette asks for the status as it opens, which also warms the
    # index and the embedding model, so the first query typed waits on neither.
    @app.get("/api/search")
    async def search_api(request: Request, q: str = "", limit: int = 12):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        q = q[:200]
        limit = max(1, min(limit, 40))
        places = await asyncio.to_thread(_search_places, app)
        found = await asyncio.to_thread(app.state.passages.search, q, place=places, limit=limit)
        return JSONResponse({"ok": True, "q": q, **found}, headers=headers)

    # The related pane (vault_ui.py): the pages nearest the one being read, out of the same index. The client names
    # the page as the reader has it, and the server works out vault-mcp's name for it, so this route can no more reach
    # a page a tree doesn't list than the palette's can.
    @app.get("/api/related")
    async def related_api(
        request: Request,
        path: str = "",
        vault_kind: str = Query("notes", alias="vault"),
        limit: int = 20,
    ):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        kind = vault_kind if vault_kind in ("notes", "html") else "notes"
        page = await asyncio.to_thread(_search_locate, app, path, kind)
        if page is None:
            return JSONResponse(
                {"ok": True, "items": [], "cut": 0, "reason": "this page isn't in Notes or Artifacts"}, headers=headers
            )
        places = await asyncio.to_thread(_search_places, app)
        found = await asyncio.to_thread(
            app.state.passages.related, page, place=places, limit=max(1, min(limit, 40))
        )
        return JSONResponse({"ok": True, **found}, headers=headers)

    @app.get("/api/search/status")
    async def search_status_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        status = await asyncio.to_thread(app.state.passages.status, warm=True)
        return JSONResponse({"ok": True, **status}, headers=cors(request.headers.get("origin")))

    # A page opened from outside (Finder, File ▸ Open, Alfred) goes to a tab of
    # its own, or to the tab already reading it (tabs_ui.py). The client can't
    # tell which vault row a real file is — the tree lists vault paths — so the
    # service maps it, as the shell does for Library's ?src= (_shell).
    @app.get("/api/vault/locate")
    async def vault_locate_api(request: Request, src: str = ""):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        found: dict = {"source": src}
        if src and not viewer.is_remote(src):
            await asyncio.to_thread(_tag_vaults, [found], "source")
        vault_kind = found.get("vault")
        return JSONResponse(
            {"ok": True, "vault": vault_kind, "path": found["vault_path"] if vault_kind else src}, headers=headers
        )

    # The sidebar's right-click menu: one row's paths to show it by, then a
    # reveal. The client only ever names a row; the server works out what that
    # row really is, so neither route can reach a path the tree doesn't list.
    @app.get("/api/vault/entry")
    async def vault_entry_api(request: Request, path: str = "", vault_kind: str = Query("notes", alias="vault")):
        if denied := api_forbidden(request):
            return denied
        headers = cors(request.headers.get("origin"))
        kind = "html" if vault_kind == "html" else "notes"
        root = _vault_root(app, kind)
        if root is None:
            return JSONResponse(
                {"ok": False, "error": _vault_missing_error(app, kind)}, status_code=400, headers=headers
            )
        paths = await asyncio.to_thread(vault.entry_paths, path, root)
        if paths is None:
            return JSONResponse({"ok": False, "error": "That is not in the vault."}, status_code=400, headers=headers)
        return JSONResponse({"ok": True, **paths}, headers=headers)

    @app.post("/api/vault/reveal")
    async def vault_reveal_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid JSON object"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        kind = "html" if body.get("vault") == "html" else "notes"
        root = _vault_root(app, kind)
        if root is None:
            return JSONResponse({"ok": False, "error": _vault_missing_error(app, kind)}, status_code=400)
        paths = await asyncio.to_thread(vault.entry_paths, str(body.get("path") or ""), root)
        if paths is None:
            return JSONResponse({"ok": False, "error": "That is not in the vault."}, status_code=400)
        # "link" shows the row from the vault's side; anything else, the real file.
        target = paths["link"] if body.get("which") == "link" else paths["real"]
        if not target:
            missing = "That row is not a link." if body.get("which") == "link" else "Its target is gone."
            return JSONResponse({"ok": False, "error": missing}, status_code=400)
        await asyncio.to_thread(vault.reveal_in_finder, target)
        return JSONResponse({"ok": True, "revealed": target}, headers=cors(request.headers.get("origin")))

    async def _html_vault_body(request: Request) -> tuple[dict, Path] | JSONResponse:
        """Shared guard for the two Artifacts write routes: token, JSON, root."""
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid JSON object"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        root = _vault_root(app, "html")
        if root is None:
            return JSONResponse({"ok": False, "error": _vault_missing_error(app, "html")}, status_code=400)
        return body, root

    @app.post("/api/vault/html/folder")
    async def html_vault_folder_api(request: Request):
        guarded = await _html_vault_body(request)
        if isinstance(guarded, JSONResponse):
            return guarded
        body, root = guarded
        try:
            folder = vault.create_folder(root, str(body.get("parent") or ""), str(body.get("name") or ""))
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        app.state.vault.invalidate()
        return JSONResponse(
            {"ok": True, "path": str(folder), "rel": folder.relative_to(root).as_posix()},
            headers=cors(request.headers.get("origin")),
        )

    @app.post("/api/vault/html/link")
    async def html_vault_link_api(request: Request):
        guarded = await _html_vault_body(request)
        if isinstance(guarded, JSONResponse):
            return guarded
        body, root = guarded
        targets = body.get("targets")
        if not isinstance(targets, list):
            targets = [body.get("target")]
        if not targets or len(targets) > 50 or not all(isinstance(t, str) and t for t in targets):
            return JSONResponse({"ok": False, "error": "Choose one to fifty HTML files or folders."}, status_code=400)
        name = body.get("name") if len(targets) == 1 and isinstance(body.get("name"), str) else None
        linked: list[str] = []
        errors: list[str] = []
        context_roots: list[str] = []
        for target in targets:
            try:
                link = vault.create_link(root, str(body.get("parent") or ""), target, name or None)
            except (OSError, ValueError) as exc:
                errors.append(f"{Path(target).name}: {exc}")
                continue
            linked.append(str(link))
            context = vault.html_context_folder(link, root)
            added = _register_context_root(app, context) if context else None
            if added is not None and str(added) not in context_roots:
                context_roots.append(str(added))
        app.state.vault.invalidate()
        status = 200 if linked else 400
        return JSONResponse(
            {
                "ok": bool(linked),
                "linked": linked,
                "context_roots": context_roots,
                "errors": errors,
                "error": "; ".join(errors) if errors and not linked else None,
            },
            status_code=status,
            headers=cors(request.headers.get("origin")),
        )

    # The sidebar's reorganising: drag a row onto a folder, and the row menu's
    # Rename, Pin to Top and Remove. Each names an entry the tree marked as the
    # vault's own; vault.owned_entry refuses anything else.
    async def _html_vault_edit(request: Request, edit) -> JSONResponse:
        guarded = await _html_vault_body(request)
        if isinstance(guarded, JSONResponse):
            return guarded
        body, root = guarded
        path = vault.normalize(str(body.get("path") or ""))
        try:
            result = await asyncio.to_thread(edit, body, root, path)
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        app.state.vault.invalidate()
        return JSONResponse({"ok": True, **result}, headers=cors(request.headers.get("origin")))

    @app.post("/api/vault/html/move")
    async def html_vault_move_api(request: Request):
        def move(body: dict, root: Path, path: Path) -> dict:
            return {"from": str(path), "path": str(vault.move_entry(root, path, str(body.get("dest") or "")))}

        return await _html_vault_edit(request, move)

    @app.post("/api/vault/html/rename")
    async def html_vault_rename_api(request: Request):
        def rename(body: dict, root: Path, path: Path) -> dict:
            return {"from": str(path), "path": str(vault.rename_entry(root, path, str(body.get("name") or "")))}

        return await _html_vault_edit(request, rename)

    @app.post("/api/vault/html/pin")
    async def html_vault_pin_api(request: Request):
        def pin(body: dict, root: Path, path: Path) -> dict:
            pinned = body.get("pinned") is not False
            return {"path": str(vault.set_pinned(root, path, pinned)), "pinned": pinned}

        return await _html_vault_edit(request, pin)

    @app.post("/api/vault/html/remove")
    async def html_vault_remove_api(request: Request):
        def remove(body: dict, root: Path, path: Path) -> dict:
            return {"path": str(path), "removed": vault.remove_entry(root, path)}

        return await _html_vault_edit(request, remove)

    @app.get("/api/settings")
    async def settings_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        storage: Storage = app.state.storage
        return JSONResponse(
            {
                "ok": True,
                "settings": storage.settings(model_default=config.model),
                "roots": storage.roots(),
            },
            headers=cors(request.headers.get("origin")),
        )

    @app.get("/api/session")
    async def session_api(request: Request):
        """Hand a trusted client the request token and the current runtime shape.

        This is no weaker than GET /ask.js, which already serves the same token
        to any origin (``Access-Control-Allow-Origin: *``) so the widget script
        tag works from a file:// page. Tightening /ask.js to the allowlist is a
        separate change; this route is gated on host + origin like the rest of
        the JSON API.
        """
        if denied := api_forbidden(request):
            return denied
        settings = app.state.storage.settings(model_default=config.model)
        return JSONResponse(
            {
                "ok": True,
                "service": "onyx",
                "protocol": PROTOCOL_VERSION,
                "version": __version__,
                "token": config.token,
                "provider": settings["provider"],
                "model": settings["model"],
                "reasoning_effort": settings["reasoning_effort"],
                "first_activity_timeout": settings["first_activity_timeout"],
                "request_timeout": settings["request_timeout"],
                "cache_ttl_hours": settings["cache_ttl_hours"],
                "cache_max_entries": settings["cache_max_entries"],
            },
            headers={**cors(request.headers.get("origin")), "Cache-Control": "no-store"},
        )

    @app.get("/api/models")
    async def models_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        settings = app.state.storage.settings(model_default=config.model)
        catalogs = await asyncio.to_thread(provider_catalogs)
        for catalog in catalogs:
            provider = str(catalog.get("id"))
            selected = str(settings.get(f"{provider}_model") or "")
            models = catalog.get("models") if isinstance(catalog.get("models"), list) else []
            if selected and not any(item.get("id") == selected for item in models):
                models.append(
                    {
                        "id": selected,
                        "label": f"{selected} (saved; unavailable)",
                        "description": "This saved model is not in the installed CLI's current catalog.",
                        "efforts": [],
                        "default_effort": settings.get(f"{provider}_effort", "medium"),
                        "unavailable": True,
                    }
                )
            catalog["models"] = models
            catalog["selected_model"] = selected
            catalog["selected_effort"] = settings.get(f"{provider}_effort", "medium")
        return JSONResponse(
            {"ok": True, "selected_provider": settings["provider"], "providers": catalogs},
            headers=cors(request.headers.get("origin")),
        )

    def current_markdown_theme() -> dict:
        settings = app.state.storage.settings(model_default=config.model)
        root = _vault_root(app)
        snapshot = vault_mode.snapshots(app.state.storage, root, settings.get("vault_mode"))[0] if root else None
        enabled = bool(settings.get("markdown_follow_obsidian", True))
        css = markdown_theme.stylesheet(snapshot) if enabled else ""
        return {"ok": True, "enabled": enabled, "available": snapshot is not None,
                "css": css, "revision": markdown_theme.revision(css)}

    @app.get("/api/markdown-theme")
    async def markdown_theme_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        return JSONResponse(current_markdown_theme(), headers={
            **cors(request.headers.get("origin")), "Cache-Control": "no-store",
        })

    async def receive_theme(request: Request, validate, save, too_large: str, outcome: dict | None = None):
        """The plugin's POST of an appearance snapshot: bounded, token-checked, validated, stored per vault.

        ``other`` (optional) is the same snapshot measured in the vault's other colour mode (vault_mode); it is
        validated as strictly, and must be the other mode. ``outcome`` (when given) keeps the last attempt, so a refused sync is visible in the status line rather
        than indistinguishable from Obsidian simply being closed.
        """
        response = await _receive_theme(request, validate, save, too_large)
        if outcome is not None and response.status_code != 403:
            error = "" if response.status_code == 200 else json.loads(response.body).get("error", "")
            outcome.update(at=time.time(), ok=response.status_code == 200, error=error)
            if error:
                logger.warning("Refused an Obsidian appearance snapshot: %s", error)
        return response

    async def _receive_theme(request: Request, validate, save, too_large: str):
        if denied := api_forbidden(request):
            return denied
        # Bound the body before decoding JSON, including chunked requests.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2 * markdown_theme.MAX_SNAPSHOT_BYTES + 8192:
                return JSONResponse({"ok": False, "error": too_large}, status_code=413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid JSON object"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        try:
            raw_root = body.get("vault_root")
            if not isinstance(raw_root, str) or len(raw_root) > 4096:
                raise ValueError("Invalid vault folder.")
            root = Path(raw_root).expanduser()
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("Vault folder does not exist.")
            snapshot = validate(body.get("snapshot"))
            other = validate(body["other"]) if body.get("other") is not None else None
            if other is not None and other["mode"] == snapshot["mode"]:
                raise ValueError("The other snapshot must be the vault's other colour mode.")
        except (OSError, ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        save(root, snapshot, other)
        return JSONResponse({"ok": True}, headers=cors(request.headers.get("origin")))

    @app.post("/api/markdown-theme")
    async def sync_markdown_theme_api(request: Request):
        return await receive_theme(
            request, markdown_theme.validate_snapshot, app.state.storage.save_markdown_theme, "Reading theme is too large."
        )

    # The vault sidebar can wear the vault's Obsidian file explorer. The snapshot
    # comes from the configured Obsidian vault and dresses both vaults' sidebars.
    def current_sidebar_theme() -> dict:
        settings = app.state.storage.settings(model_default=config.model)
        root = _vault_root(app)
        snapshot = vault_mode.snapshots(app.state.storage, root, settings.get("vault_mode"))[1] if root else None
        enabled = bool(settings.get("sidebar_follow_obsidian", True))
        css = sidebar_theme.stylesheet(snapshot) if enabled else ""
        folders = sidebar_theme.folder_tints(snapshot) if css else []
        return {"ok": True, "enabled": enabled, "available": snapshot is not None,
                "css": css, "folders": folders, "revision": sidebar_theme.revision(css, folders),
                "last_sync": dict(sidebar_sync) or None}

    sidebar_sync: dict = {}  # the plugin's last sidebar POST: {at, ok, error}

    @app.get("/api/sidebar-theme")
    async def sidebar_theme_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        return JSONResponse(current_sidebar_theme(), headers={
            **cors(request.headers.get("origin")), "Cache-Control": "no-store",
        })

    @app.post("/api/sidebar-theme")
    async def sync_sidebar_theme_api(request: Request):
        return await receive_theme(
            request, sidebar_theme.validate_snapshot, app.state.storage.save_sidebar_theme, "Sidebar theme is too large.",
            outcome=sidebar_sync,
        )

    # The whole app in the vault's colours (vault_look): the palette the two snapshots give, while either
    # vault-appearance setting is on — Settings shows them as one switch, "Match vault appearance". Which of the
    # vault's colour modes is Settings' Color theme while it is on (vault_mode); `modes` is what the plugin has
    # measured, so Settings can say when the one asked for hasn't arrived.
    def current_vault_look() -> dict:
        settings = app.state.storage.settings(model_default=config.model)
        root = _vault_root(app)
        enabled = bool(settings.get("markdown_follow_obsidian", True) or settings.get("sidebar_follow_obsidian", True))
        look = vault_look.palette(*vault_mode.snapshots(app.state.storage, root, settings.get("vault_mode"))) if root else None
        worn = look if enabled else None
        return {"ok": True, "enabled": enabled, "page_enabled": bool(settings.get("html_follow_page", True)),
                "available": look is not None, "choice": settings.get("vault_mode") or "obsidian",
                "wanted": vault_mode.wanted(settings.get("vault_mode")),
                "modes": vault_mode.available(app.state.storage, root) if root else [],
                "mode": worn["mode"] if worn else None, "base": worn["base"] if worn else None,
                "css": vault_look.stylesheet(worn), "reader_css": vault_look.reader_stylesheet(worn),
                "revision": vault_look.revision(worn)}

    # A page comes already as it will look, and knowing where the reader left it, so nothing about it changes after its
    # first frame: the vault look's stylesheet, and its mode on <html> (its rules key on html[data-askw-look]); the app
    # theme; the remembered scroll position. HTML may then override the controls with its measured page colours.
    # ask.js takes the baseline at boot (seedLook, initPosition) instead of fetching it and
    # restyling or jumping once the page had painted, which made every change of page look jerky.
    def first_paint(html_text: str, source: str, settings: dict, *, kind: str = "", shown: str | None = None) -> str:
        look, row = current_vault_look(), app.state.storage.document(source)
        head = (f'<meta name="askw-page-enabled" content="{str(bool(settings.get("html_follow_page", True))).lower()}">'
                f'<meta name="askw-kind" content="{_esc(kind)}">'
                f'<meta name="askw-appearance" content="{_esc(str(settings.get("appearance_theme") or "system"))}">'
                f'<meta name="askw-scroll" content="{float(row["scroll_y"]) if row else 0.0:g}">')
        if shown:
            head += f'<meta name="askw-doc-sig" content="{_esc(shown)}">'
        if look["reader_css"]:
            mode = _esc(look["mode"])
            head += (f'<meta name="askw-look" content="{mode}"><meta name="askw-look-revision" content="{_esc(look["revision"])}">'
                     f'<style id="askw-vault-look">{look["reader_css"]}</style>')
            html_text = _HTML_TAG_RE.sub(
                lambda m: m.group(0)[:-1].rstrip("/") + f' data-askw-look="{mode}" data-askw-color="{mode}">', html_text, count=1
            )
        # Into the head; a page without one takes it just after its doctype, never before it (that would put the page
        # in quirks mode, where a table stops inheriting the page's text colour).
        end = _HEAD_END_RE.search(html_text)
        at = end.start() if end else (m.end() if (m := _DOCTYPE_RE.match(html_text)) else 0)
        return html_text[:at] + head + html_text[at:]

    @app.get("/api/vault-look")
    async def vault_look_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        return JSONResponse(current_vault_look(), headers={
            **cors(request.headers.get("origin")), "Cache-Control": "no-store",
        })

    @app.post("/api/page-look")
    async def page_look_api(request: Request):
        # A pure colour calculation: no paths, document reads, or saved theme. Only
        # bounded RGB values enter the existing contrast-checked palette builder.
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        colors = {key: value for key in ("background", "ink", "link")
                  if isinstance(value := body.get(key), str) and len(value) <= 128}
        look = vault_look.palette({"styles": {
            "content": {"background-color": colors.get("background"), "color": colors.get("ink")},
            "a": {"color": colors.get("link") or vault_look.ONYX_BLUE},
        }}, None) if app.state.storage.settings().get("html_follow_page", True) else None
        return JSONResponse({"ok": True, "page": True, "mode": look["mode"] if look else None,
                             "base": look["base"] if look else None, "css": vault_look.stylesheet(look),
                             "reader_css": vault_look.reader_stylesheet(look), "revision": vault_look.revision(look)},
                            headers={**cors(request.headers.get("origin")), "Cache-Control": "no-store"})

    @app.post("/api/settings")
    async def update_settings_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        patch = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        if any(
            key in patch
            for key in ("provider", "model", "claude_model", "codex_model", "claude_effort", "codex_effort")
        ):
            current = app.state.storage.settings(model_default=config.model)
            provider = str(patch.get("provider") or current["provider"])
            model = str(
                patch.get(f"{provider}_model")
                or patch.get("model")
                or current.get(f"{provider}_model")
                or ""
            )
            effort = str(patch.get(f"{provider}_effort") or current.get(f"{provider}_effort") or "medium")
            catalogs = await asyncio.to_thread(provider_catalogs)
            catalog = next((item for item in catalogs if item.get("id") == provider), None)
            available = catalog.get("models", []) if catalog else []
            if available and model not in {item.get("id") for item in available}:
                return JSONResponse(
                    {"ok": False, "error": f"{model!r} is not available for {provider}. Refresh the model catalog."},
                    status_code=400,
                )
            selected = next((item for item in available if item.get("id") == model), None)
            supported_efforts = selected.get("efforts", []) if selected else []
            if supported_efforts and effort not in supported_efforts:
                return JSONResponse(
                    {"ok": False, "error": f"{effort!r} effort is not supported by {model}."},
                    status_code=400,
                )
        try:
            settings = app.state.storage.update_settings(
                patch,
                model_default=config.model,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if "vault_root" in patch and settings.get("vault_root"):
            app.state.storage.add_root(Path(str(settings["vault_root"])))
        app.state.vault.invalidate()
        return {"ok": True, "settings": settings}

    @app.post("/api/roots")
    async def add_root_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        body = await request.json()
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        try:
            path = Path(str(body.get("path") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid folder"}, status_code=400)
        if not path.is_dir():
            return JSONResponse({"ok": False, "error": "folder does not exist"}, status_code=400)
        app.state.storage.add_root(path)
        return JSONResponse(
            {"ok": True, "roots": app.state.storage.roots()},
            headers=cors(request.headers.get("origin")),
        )

    @app.delete("/api/roots")
    async def remove_root_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        body = await request.json()
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        try:
            path = Path(str(body.get("path") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid folder"}, status_code=400)
        if not app.state.storage.remove_root(path):
            return JSONResponse({"ok": False, "error": "built-in roots cannot be removed"}, status_code=400)
        return {"ok": True, "roots": app.state.storage.roots()}

    @app.get("/api/diagnostics")
    async def diagnostics_api(request: Request, probe: bool = False):
        if denied := api_forbidden(request):
            return denied
        settings = app.state.storage.settings(model_default=config.model)
        result = await asyncio.to_thread(
            build_diagnostics,
            app.state.storage,
            provider=settings["provider"],
            model=settings["model"],
            effort=settings["reasoning_effort"],
            roots=app.state.storage.roots(),
            default_folder=config.default_folder,
            probe=probe,
        )
        return JSONResponse(result)

    def setup_state() -> dict:
        settings = app.state.storage.settings(model_default=config.model)
        root = _vault_root(app)
        artifacts = _vault_root(app, "html")
        return {
            "ok": True,
            "dismissed": bool(settings.get("setup_dismissed")),
            "vault": {"ok": root is not None, "path": str(root) if root else str(settings.get("vault_root") or "")},
            "artifacts": {"ok": artifacts is not None, "path": str(artifacts) if artifacts
                          else str(settings.get("html_vault_root") or "")},
            "plugin": first_run.plugin_status(root),
            "theme": {"ok": root is not None and app.state.storage.markdown_theme(root) is not None},
        }

    @app.get("/api/setup")
    async def setup_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        return JSONResponse(await asyncio.to_thread(setup_state), headers={"Cache-Control": "no-store"})

    @app.post("/api/setup/obsidian-plugin")
    async def install_obsidian_plugin_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict) or body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        root = _vault_root(app)
        if root is None:
            return JSONResponse({"ok": False, "error": "Choose your Obsidian vault first."}, status_code=400)
        try:
            await asyncio.to_thread(first_run.install_plugin, root)
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(await asyncio.to_thread(setup_state))

    @app.post("/api/position")
    async def update_position_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        body = await request.json()
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        source = str(body.get("source") or "")[:4000]
        if not app.state.storage.document(source):
            return JSONResponse({"ok": False, "error": "unknown document"}, status_code=404)
        app.state.storage.update_position(source, float(body.get("scroll_y") or 0))
        return {"ok": True}

    @app.get("/api/export")
    async def export_api(request: Request, src: str):
        if denied := api_forbidden(request):
            return denied
        try:
            markdown = app.state.storage.export_markdown(src)
        except KeyError:
            return Response("Document not found", status_code=404)
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(src).stem or "reading-notes")
        return Response(
            markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}-notes.md"'},
        )

    def _reader_href(path: Path, context: Path | None) -> str:
        """The reader URL a cited file opens at: its row when a vault lists it, so the sidebar highlights it and it
        reads with that vault's context (as a Finder open does); otherwise the file itself, in the answer's context."""
        params = {"src": str(path), **({"folder": str(context)} if context is not None else {})}
        for kind in ("notes", "html"):
            root = _vault_root(app, kind)
            row = app.state.vault.get(root, kind).by_real(path) if root is not None else None
            if row is not None:
                # As the shell's viewHref reads a row: a note with its vault as the folder, a page with its own.
                params = {"src": str(row.path), **({"folder": str(root)} if kind == "notes" else {})}
                break
        return "/view?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/")

    @app.post("/api/open-source")
    async def open_source_api(request: Request):
        if denied := api_forbidden(request):
            return denied
        body = await request.json()
        if body.get("token") != config.token:
            return JSONResponse({"ok": False, "error": "invalid token"}, status_code=403)
        try:
            path = Path(str(body.get("path") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid source path"}, status_code=400)
        folder = _resolve_folder(app, body.get("folder"))
        inside_folder = bool(folder and (path == folder or path.is_relative_to(folder)))
        known_document = app.state.storage.document(str(path)) is not None
        if not inside_folder and not known_document:
            return JSONResponse({"ok": False, "error": "source is outside the active context"}, status_code=403)
        try:
            line = int(body["line"]) if body.get("line") else None
            page = int(body["page"]) if body.get("page") else None
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid line or page"}, status_code=400)
        if body.get("reader") and path.suffix.lower() in viewer.LOCAL_DOCUMENT_EXTENSIONS:
            # A page or note is read in Onyx, at the cited passage: say where, and the widget goes there. Code, which
            # the reader doesn't show, still opens in the editor below.
            if not path.is_file():
                return JSONResponse({"ok": False, "error": f"Source no longer exists: {path}"}, status_code=400)
            href = await asyncio.to_thread(_reader_href, path, folder)
            target = await asyncio.to_thread(reader_target, path, line=line, page=page)
            return JSONResponse({"ok": True, "view": href, **target}, headers=cors(request.headers.get("origin")))
        try:
            await asyncio.to_thread(open_source, path, line=line, page=page)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True}, headers=cors(request.headers.get("origin")))

    @app.options("/ask")
    async def ask_preflight(request: Request):
        return Response(status_code=204, headers=cors(request.headers.get("origin")))

    @app.options("/api/{path:path}")
    async def api_preflight(request: Request, path: str):
        return Response(status_code=204, headers=cors(request.headers.get("origin")))

    @app.options("/open-in-provider")
    @app.options("/open-in-claude")
    async def open_in_provider_preflight(request: Request):
        return Response(status_code=204, headers=cors(request.headers.get("origin")))

    @app.post("/open-in-provider")
    @app.post("/open-in-claude")
    async def open_in_provider(request: Request):
        # Same gating as /ask — this spawns a terminal agent session, a real
        # side effect. All app-level outcomes return 200 + {ok,...} so the widget
        # (cross-origin or same-origin) can read them.
        origin = request.headers.get("origin")
        headers = cors(origin)

        def reply(payload, status=200):
            return JSONResponse(payload, status_code=status, headers=headers)

        if not origin_ok(origin):
            return reply({"ok": False, "error": "origin not allowed"})
        if not _host_allowed(request.headers.get("host", ""), config.port):
            return reply({"ok": False, "error": "host not allowed"})
        try:
            body = await request.json()
        except Exception:
            return reply({"ok": False, "error": "invalid JSON"})
        if body.get("token") != config.token:
            return reply({"ok": False, "error": "invalid token"})

        settings = app.state.storage.settings(model_default=config.model)
        provider = str(body.get("provider") or settings["provider"])
        if provider not in {"claude", "codex"}:
            return reply({"ok": False, "error": "unknown provider"})
        runtime = await asyncio.to_thread(provider_status, provider)
        if not runtime.get("subscription"):
            return reply({"ok": False, "error": runtime.get("repair") or "subscription login required"})

        folder = _resolve_folder(app, body.get("folder"))
        if folder is None:
            return reply({"ok": False, "error": f"folder not allowed: {body.get('folder')!r}"})

        prompt = build_handoff_prompt(
            folder,
            body.get("action"),
            body.get("selection") or "",
            body.get("question") or "",
            body.get("answer") or "",
            body.get("context") or "",
        )
        if body.get("mode") == "copy":
            return reply({"ok": True, "prompt": prompt})

        try:
            await asyncio.to_thread(handoff.open_in_provider, provider, folder, prompt)
        except Exception as exc:
            return reply({"ok": False, "error": str(exc), "prompt": prompt})
        return reply({"ok": True, "opened": True, "prompt": prompt})

    @app.post("/ask")
    async def ask(request: Request):
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")

        if not origin_ok(origin):
            return err_stream("Refused: origin not allowed.", origin)
        if not _host_allowed(host, config.port):
            return err_stream("Refused: host not allowed (possible DNS-rebinding).", origin)

        try:
            body = await request.json()
        except Exception:
            return err_stream("Invalid JSON body.", origin)

        if body.get("token") != config.token:
            return err_stream("Refused: invalid or missing token.", origin)

        action = body.get("action")
        if action not in ("eli5", "prove", "ask"):
            return err_stream("Unknown action.", origin)

        selection = (body.get("selection") or "").strip()
        document_source = str(body.get("document_source") or "")[:4000] or None
        if not selection and (action != "ask" or not document_source):
            return err_stream("No text was selected.", origin)
        selection = selection[:MAX_SELECTION]
        context = (body.get("context") or "")[:MAX_CONTEXT]

        question = (body.get("question") or "").strip()
        if action == "ask" and not question:
            return err_stream("No question was provided.", origin)

        request_mode = str(body.get("request_mode") or "generated")
        if request_mode not in {"generated", "rerun", "edited", "continue"}:
            request_mode = "generated"
        parent_request_id = str(body.get("parent_request_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parent_request_id):
            parent_request_id = ""

        folder = _resolve_folder(app, body.get("folder"))
        if folder is None:
            return err_stream(
                f"Refused: folder not allowed or not a directory: {body.get('folder')!r}", origin
            )
        _remember_folder(app, str(folder))

        history = _sanitize_history(body.get("history"))
        document_title = str(body.get("document_title") or "")[:500] or None
        try:
            document_page = int(body.get("document_page")) if body.get("document_page") else None
        except (TypeError, ValueError):
            document_page = None
        settings = app.state.storage.settings(model_default=config.model)
        provider = settings["provider"]
        model = settings["model"]
        effort = settings["reasoning_effort"]
        prompt = build_user_prompt(
            action,
            selection,
            context,
            question,
            history,
            document_source=document_source,
            document_page=document_page,
        )
        web = bool(settings["web_lookups"])
        append_system = append_system_for(action, settings["response_style"], web)
        sem: asyncio.Semaphore = app.state.sem
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        document_id = None
        if document_source:
            existing = app.state.storage.document(document_source)
            if existing:
                document_id = existing["id"]
            else:
                kind = "remote" if viewer.is_remote(document_source) else Path(document_source).suffix.lstrip(".") or "web"
                document_id = app.state.storage.upsert_document(
                    source=document_source,
                    title=document_title or Path(document_source).name or document_source,
                    kind=kind,
                    folder=str(folder),
                )
        if settings["history_enabled"]:
            app.state.storage.start_conversation(
                request_id=request_id,
                document_id=document_id,
                document_source=document_source,
                document_title=document_title,
                document_page=document_page,
                selection=selection,
                context=context,
                action=action,
                question=question,
                folder=str(folder),
                provider=provider,
                model=model,
                effort=effort,
                request_mode=request_mode,
                parent_request_id=parent_request_id or None,
            )

        async def gen():
            acquired = False
            answer = ""
            error = ""
            citations: list[dict] = []
            trace: list[dict] = []
            status = "error"
            try:
                yield _sse(
                    "meta",
                    {
                        "request_id": request_id,
                        "provider": provider,
                        "model": model,
                        "effort": effort,
                        "request_mode": request_mode,
                        "parent_request_id": parent_request_id or None,
                    },
                )
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=0.05)
                    acquired = True
                except asyncio.TimeoutError:
                    error = "Server busy (too many concurrent requests). Try again in a moment."
                    yield _sse(
                        "error",
                        {"message": error, "retryable": True},
                    )
                    return
                async for chunk in stream_answer(
                    provider,
                    prompt,
                    folder,
                    model,
                    append_system,
                    effort=effort,
                    web=web,
                    document_source=document_source,
                    first_activity_timeout=float(settings["first_activity_timeout"]),
                    stream_timeout=float(settings["request_timeout"]),
                ):
                    event, data = _decode_sse(chunk)
                    if event == "token":
                        answer += str(data.get("text") or "")
                    elif event == "tool_trace":
                        trace.append(data)
                    elif event == "citations":
                        citations = data.get("items") if isinstance(data.get("items"), list) else []
                    elif event == "error":
                        error = str(data.get("message") or f"{provider.title()} reported an error.")
                    elif event == "done":
                        status = "complete"
                    yield chunk
            except asyncio.CancelledError:
                status = "cancelled"
                error = "Request cancelled by the reader."
                raise
            except Exception as exc:
                error = f"Unexpected request failure: {exc}"
                yield _sse("error", {"message": error, "retryable": True})
            finally:
                if acquired:
                    sem.release()
                if settings["history_enabled"]:
                    app.state.storage.finish_conversation(
                        request_id,
                        status=status,
                        answer=answer,
                        error=error,
                        citations=citations,
                        trace=trace,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={**cors(origin), **_SSE_HEADERS, "X-Request-ID": request_id},
        )

    return app

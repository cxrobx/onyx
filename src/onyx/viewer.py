"""Wrap any HTML (remote URL or local file) so the widget can run against it.

The widget needs three things a foreign page won't give it on its own:
  * to be **same-origin** with the server (so the `fetch` POST to /ask is allowed
    and not blocked as cross-origin or mixed-content);
  * its **assets resolved** (these docs pull a relative `../.assets/dossier.css`,
    which would 404 — and render unstyled — if served naively);
  * the **widget `<script>` injected**.

`prepare_html` does all three: it rewrites `<link>/<script>/<img>/<source>/<audio>/<video>/<track>` asset
refs to absolute URLs (remote → the original site; local → a `/_fs/...` route that
serves the file from disk), strips any `<base>`/CSP that would break same-origin
anchors or block the widget, and injects the widget script before `</body>`.
Remote documents are always made inert. Local HTML can explicitly keep its scripts
so interactive artifacts behave like they do when opened directly in a browser.
"""

from __future__ import annotations

import codecs
import html as _html
import ipaddress
import os
import re
import socket
import stat
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from markdown_it import MarkdownIt
from markdown_it.common import normalize_url as _normalize_url
from markdown_it.rules_inline.state_inline import Delimiter
from markdown_it.token import Token

from . import __version__

if TYPE_CHECKING:  # vault.py imports from this module; keep the runtime import one-way
    from .vault import VaultIndex

# Match a <link|script|img|source|audio|video|track ...> tag's first href/src
# value. The quote class is symmetric (group 2 = opening quote, back-referenced as
# the closing quote) so single-quoted attributes are handled too. These docs are
# generated and well-formed, so a scoped regex beats dragging in an HTML parser
# that would round-trip-mangle the markup. Media tags are here because study
# guides narrate with a bare `<audio src="audio/x.m4a">`, which would otherwise
# resolve against /view and 404.
_ASSET_RE = re.compile(
    r"""(<(?:link|script|img|source|audio|video|track)\b[^>]*?\b(?:href|src)=(["']))(.*?)\2""",
    re.IGNORECASE,
)
# Used to neutralize a viewed document's own scripts when the caller has not
# explicitly trusted a local HTML file. Re-serving foreign HTML same-origin on
# localhost would otherwise let its inline JS read /_fs and /ask and exfiltrate.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/>", re.IGNORECASE | re.DOTALL)
_BASE_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_CSP_RE = re.compile(
    r'<meta\b[^>]*http-equiv=["\']?content-security-policy["\']?[^>]*>',
    re.IGNORECASE,
)
_BODY_RE = re.compile(r"</body>", re.IGNORECASE)
_EMBEDDED_BASE_HREF_RE = re.compile(r'<base\b[^>]*\bhref=["\']([^"\']*)["\']', re.IGNORECASE)

_SKIP_PREFIXES = ("http://", "https://", "data:", "//", "#", "mailto:", "tel:", "javascript:")

MAX_REMOTE_BYTES = 12 * 1024 * 1024
LOCAL_DOCUMENT_EXTENSIONS = {".html", ".htm", ".md", ".markdown", ".txt", ".pdf"}

# Note: .json / .map deliberately excluded — serving them would turn /_fs into a
# reader for secret-bearing config (.docker/config.json, ~/.claude.json, …). /_fs
# is additionally locked to the exact asset files a viewed document references.
ASSET_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".vtt": "text/vtt",
}


class RangeNotSatisfiable(ValueError):
    """A ``Range`` header that asks for bytes the file does not have (HTTP 416)."""


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse one ``Range: bytes=…`` header into an inclusive ``(start, end)``.

    WebKit will not play ``<audio>``/``<video>`` from a server that ignores
    ranges: it opens with ``bytes=0-1`` and gives up on a plain 200. So /_fs
    answers single ranges with 206. Returns None when the whole file should be
    sent — no header, a unit other than bytes, a multi-range list, or a header
    too malformed to mean anything (RFC 9110 lets a server ignore those).
    Raises :class:`RangeNotSatisfiable` for a well-formed range past the end.
    """
    if not header:
        return None
    unit, sep, spec = header.partition("=")
    if not sep or unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return None
    first, last = first.strip(), last.strip()
    if not (first.isdigit() or first == "") or not (last.isdigit() or last == "") or (first == last == ""):
        return None
    if first == "":  # suffix: the last N bytes
        length = int(last)
        if length == 0 or size == 0:
            raise RangeNotSatisfiable(header)
        return max(0, size - length), size - 1
    start = int(first)
    if last and int(last) < start:
        return None  # backwards, so malformed rather than unsatisfiable
    if start >= size:
        raise RangeNotSatisfiable(header)
    end = int(last) if last else size - 1
    return start, min(end, size - 1)


class ViewerError(Exception):
    """Raised with a user-facing message when a source can't be loaded."""


@dataclass(frozen=True)
class LoadedDocument:
    html: str
    title: str
    kind: str
    page_count: int | None = None


@dataclass
class RenderContext:
    """What the Markdown renderer needs to turn links into reader URLs.

    ``doc_path`` is the **lexical** path of the document (a note under a
    symlinked vault folder keeps its vault-visible path here); ``vault`` is set
    only when the document lives inside the configured vault.
    """

    doc_path: Path
    folder: str | None = None
    vault: "VaultIndex | None" = None

    def view_url(self, target: Path | str) -> str:
        params = {"src": str(target)}
        if self.folder:
            params["folder"] = self.folder
        # Encode exactly once; a literal ``%`` in a filename round-trips as ``%25``.
        return "/view?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/")

    def rewrite_href(self, href: str) -> str:
        """Point a relative/absolute/file: link at a local document to /view."""
        if not href or href.startswith("#"):
            return href
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme and parsed.scheme.lower() != "file":
            return href
        raw = urllib.parse.unquote(parsed.path)
        if not raw:
            return href
        if Path(raw).suffix.lower() not in LOCAL_DOCUMENT_EXTENSIONS:
            return href
        if raw.startswith("/"):
            target = Path(os.path.normpath(raw))
            if not target.is_file():
                return href
        else:
            target = Path(os.path.normpath(str(self.doc_path.parent / raw)))
        url = self.view_url(target)
        if parsed.fragment:
            url += "#" + parsed.fragment
        return url


def is_remote(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def validate_remote_url(url: str, *, allow_private: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ViewerError("Only http:// and https:// document URLs are supported.")
    if parsed.username or parsed.password:
        raise ViewerError("Document URLs containing credentials are not allowed.")
    if allow_private:
        return url
    try:
        records = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise ViewerError(f"Could not resolve {parsed.hostname}: {exc}") from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ViewerError(
                f"Remote documents may not resolve to private/local addresses ({address}). "
                "This can be changed in Settings for trusted development URLs."
            )
    return url


class _ValidatedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_remote_url(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_remote(url: str, *, allow_private: bool = False) -> str:
    validate_remote_url(url, allow_private=allow_private)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"Onyx/{__version__} (+local reading companion)"},
    )
    try:
        opener = urllib.request.build_opener(_ValidatedRedirects(allow_private))
        with opener.open(req, timeout=20) as resp:  # noqa: S310 - validated above
            content_type = (resp.headers.get_content_type() or "").lower()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise ViewerError(f"Remote URL returned unsupported content type: {content_type}")
            raw = resp.read(MAX_REMOTE_BYTES + 1)
    except ViewerError:
        raise
    except Exception as exc:  # urllib raises a zoo of error types
        raise ViewerError(f"Could not fetch {url}: {exc}") from exc
    if len(raw) > MAX_REMOTE_BYTES:
        raise ViewerError(f"Remote document is larger than {MAX_REMOTE_BYTES // (1024 * 1024)} MB.")
    return raw.decode("utf-8", errors="replace")


def read_local(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise ViewerError(f"No such file: {path}")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ViewerError(f"Could not read {path}: {exc}") from exc


# MARK: - Editing a note (⌘E, editor/)

EDITABLE_EXTENSIONS = {".md", ".markdown"}
MAX_EDIT_BYTES = 8 * 1024 * 1024


class SourceConflict(ViewerError):
    """The note on disk is no longer the version a save was written against."""

    def __init__(self, sig: str) -> None:
        super().__init__("This note changed on disk.")
        self.sig = sig


def stat_signature(st: os.stat_result) -> str:
    """What live reload and the editor compare to tell one version of a file from the next."""
    return f"{st.st_mtime_ns}:{st.st_size}"


# One lock per stripe of note paths: a save's check of the version and its write are one step, so of two saves
# written against the same version (two tabs, a task tick beside an autosave) the second is refused, not interleaved.
_NOTE_LOCKS = tuple(threading.RLock() for _ in range(64))


@contextmanager
def _note_lock(path: Path):
    with _NOTE_LOCKS[hash(str(path)) % len(_NOTE_LOCKS)]:
        yield


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_note(path: Path, flags: int) -> int:
    """Open the one file a capability names. ``path`` is the realpath the page was opened with, so it must still be
    one: a folder along it turned into a symlink since would lead the same path to another note. ``O_NOFOLLOW`` only
    guards the last part, so the whole path is checked, before the open and again after it."""
    if os.path.realpath(path) != str(path):
        raise ViewerError("This note has moved since its page was opened; reload the page.")
    fd = os.open(path, flags | os.O_NOFOLLOW)
    if os.path.realpath(path) != str(path):
        os.close(fd)
        raise ViewerError("This note has moved since its page was opened; reload the page.")
    return fd


def read_source(path: Path) -> tuple[str, str]:
    """A note's text for the editor, and the signature of the version it is.

    Strictly UTF-8: the reader decodes with replacement characters, which is fine to look at but would be saved back
    over the bytes they stand in for. A byte-order mark is left out and CRLF comes back as LF, as the editor keeps
    text; ``write_source`` puts both back.
    """
    if path.suffix.lower() not in EDITABLE_EXTENSIONS:
        raise ViewerError("Only Markdown notes can be edited.")
    try:
        with os.fdopen(_open_note(path, os.O_RDONLY), "rb") as handle:
            st = os.fstat(handle.fileno())
            if st.st_size > MAX_EDIT_BYTES:
                raise ViewerError("This note is too large to edit here.")
            data = handle.read()
    except FileNotFoundError:
        raise ViewerError(f"No such file: {path}") from None
    except OSError as exc:
        raise ViewerError(f"Could not read {path}: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ViewerError("This note isn’t UTF-8 text, so Onyx won’t edit it.") from None
    return text.removeprefix("\ufeff").replace("\r\n", "\n"), stat_signature(st)


def write_source(path: Path, text: str, *, base: str) -> str:
    """Save the editor's text over the version ``base`` names, and return the signature of the one written.

    In place, never a new file renamed over the old: a rename gives the note a new inode and so a new creation date,
    which Obsidian shows and sorts notes by. The new bytes go over the old before the file is cut to length, so a
    watcher never reads it empty. A file that ended its lines in CRLF, or began with a byte-order mark, keeps doing so.

    Another writer can still swap the file under it by renaming its own copy in (a sync can). Then the path no longer
    names the file this wrote, whose bytes went nowhere, so that is a conflict, never a success.
    """
    if path.suffix.lower() not in EDITABLE_EXTENSIONS:
        raise ViewerError("Only Markdown notes can be edited.")
    encoded = text.replace("\r\n", "\n").encode("utf-8")
    if len(encoded) > MAX_EDIT_BYTES:
        raise ViewerError("This note is too large to save here.")

    def at_path() -> os.stat_result:
        try:
            return os.stat(path)
        except FileNotFoundError:
            raise ViewerError("This note is no longer on disk.") from None

    with _note_lock(path):
        try:
            fd = _open_note(path, os.O_RDWR)
        except FileNotFoundError:
            raise ViewerError("This note is no longer on disk.") from None
        except OSError as exc:
            raise ViewerError(f"Could not open {path.name} to save it: {exc.strerror or exc}") from exc
        with os.fdopen(fd, "r+b") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ViewerError("Only a file can be edited.")
            now = at_path()
            if not _same_file(st, now):
                raise SourceConflict(stat_signature(now))
            if stat_signature(st) != base:
                raise SourceConflict(stat_signature(st))
            current = handle.read()
            if b"\r\n" in current and current.count(b"\r\n") == current.count(b"\n"):
                encoded = encoded.replace(b"\n", b"\r\n")
            if current.startswith(codecs.BOM_UTF8):
                encoded = codecs.BOM_UTF8 + encoded
            if encoded != current:
                handle.seek(0)
                handle.write(encoded)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            written, now = os.fstat(handle.fileno()), at_path()
            if not _same_file(written, now):
                raise SourceConflict(stat_signature(now))
            return stat_signature(written)


# A task item's line: any quote and list markers, then the box. Group 2 is the character inside it.
_TASK_LINE_RE = re.compile(r"^((?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\[)([ xX])(\][ \t])")


def set_task(path: Path, line: int, done: bool, *, base: str) -> str:
    """Tick or clear the task on ``line`` (0-based, as the page's box names it), over the version ``base`` names.

    The read and the write are one step under the note's lock, so the line ticked is the line read."""
    with _note_lock(path):
        text, sig = read_source(path)
        if sig != base:
            raise SourceConflict(sig)
        lines = text.split("\n")
        if not 0 <= line < len(lines):
            raise ViewerError("That task is no longer in this note.")
        found = _TASK_LINE_RE.match(lines[line])
        if not found:
            raise ViewerError("That task is no longer in this note.")
        lines[line] = found.group(1) + ("x" if done else " ") + lines[line][found.end(2):]
        return write_source(path, "\n".join(lines), base=base)


# MARK: - Markdown: frontmatter, wikilinks, link rewriting

# Obsidian-style YAML frontmatter: an opening ``---`` on the very first line and
# a closing ``---``/``...`` line. Only matched at offset 0.
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?:(.*?)\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", re.DOTALL
)
_YAML_KEY_RE = re.compile(r"^([A-Za-z0-9_][\w .\-/]*?)\s*:(?:\s+(.*))?$")
_FLOW_ITEM_RE = re.compile(r'"[^"]*"|\'[^\']*\'|[^,]+')
_TAG_KEYS = {"tags", "tag"}
MAX_FRONTMATTER_LINES = 60


def _yaml_shaped(block: str) -> bool:
    """Every top-level line is ``key:`` or ``- item``; indented lines are free."""
    saw_key = False
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            continue
        if _YAML_KEY_RE.match(line):
            saw_key = True
            continue
        return False
    return saw_key


def split_frontmatter(raw: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_block, body)``; block is None when there is none.

    A leading ``---`` that is not followed by a YAML-shaped block and a closing
    fence is an ordinary horizontal rule and never swallows content.
    """
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return None, raw
    block = match.group(1) or ""
    if block.strip() and not _yaml_shaped(block):
        return None, raw
    return block, raw[match.end():]


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_flat_frontmatter(block: str) -> dict[str, str | list[str]] | None:
    """Parse ``key: value`` / flow lists / block lists. ``None`` when nested."""
    lines = block.splitlines()
    if len(lines) > MAX_FRONTMATTER_LINES:
        return None
    fields: dict[str, str | list[str]] = {}
    list_key: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            if list_key is None:
                return None
            item = _unquote_scalar(stripped[1:])
            existing = fields[list_key]
            assert isinstance(existing, list)
            if item:
                existing.append(item)
            continue
        if line[:1] in (" ", "\t"):
            return None  # nested mapping or multi-line scalar
        match = _YAML_KEY_RE.match(line)
        if not match:
            return None
        key = match.group(1).strip()
        value = (match.group(2) or "").strip()
        if not value:
            fields[key] = []
            list_key = key
            continue
        list_key = None
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [
                _unquote_scalar(item) for item in _FLOW_ITEM_RE.findall(inner) if item.strip()
            ] if inner else []
        else:
            fields[key] = _unquote_scalar(value)
    return fields


def _render_properties(block: str, md: MarkdownIt, env: dict[str, Any]) -> str:
    fields = parse_flat_frontmatter(block)
    if fields is None:
        return (
            '<details class="askw-properties"><summary>Properties</summary>'
            f"<pre>{_html.escape(block.strip())}</pre></details>"
        )
    if not fields:
        return ""

    def inline(text: str) -> str:
        return md.renderInline(text, env)

    rows: list[str] = []
    for key, value in fields.items():
        if isinstance(value, list):
            if not value:
                cell = '<span class="askw-empty">—</span>'
            elif key.casefold() in _TAG_KEYS:
                cell = "".join(
                    f'<span class="askw-tag">#{_html.escape(item.lstrip("#"))}</span>' for item in value
                )
            else:
                cell = ", ".join(inline(item) for item in value)
        elif key.casefold() in _TAG_KEYS:
            cell = "".join(
                f'<span class="askw-tag">#{_html.escape(item.strip().lstrip("#"))}</span>'
                for item in re.split(r"[,\s]+", value)
                if item.strip()
            )
        else:
            cell = inline(value) if value else '<span class="askw-empty">—</span>'
        rows.append(f"<dt>{_html.escape(key)}</dt><dd>{cell}</dd>")
    return (
        f'<details class="askw-properties"><summary>Properties · {len(rows)}</summary>'
        f"<dl>{''.join(rows)}</dl></details>"
    )


def _validate_link(url: str) -> bool:
    # markdown-it rejects ``file:`` outright; the reader turns those into /view
    # links, so allow the scheme while keeping javascript:/vbscript:/data: out.
    if url.strip().lower().startswith("file:"):
        return True
    return _normalize_url.validateLink(url)


def _wikilink_rule(state: Any, silent: bool) -> bool:
    """``[[Note]]``, ``[[Note|Alias]]``, ``[[Note#Heading]]``, ``![[image.png]]``."""
    ctx = (state.env or {}).get("askw") if state.env is not None else None
    if ctx is None or ctx.vault is None:
        return False  # non-vault markdown keeps the literal text
    src: str = state.src
    pos: int = state.pos
    embed = False
    if src.startswith("![[", pos):
        embed = True
        start = pos + 3
    elif src.startswith("[[", pos):
        start = pos + 2
    else:
        return False
    end = src.find("]]", start, state.posMax)
    if end < 0:
        return False
    inner = src[start:end]
    if not inner.strip() or "\n" in inner or "[[" in inner:
        return False
    if silent:
        return True
    target, _, alias = inner.partition("|")
    target = target.strip()
    alias = alias.strip()
    base, _, heading = target.partition("#")
    base = base.strip()
    label = alias or target
    vault = ctx.vault
    resolved = None
    if base:
        resolved = vault.resolve_embed(base, source=ctx.doc_path) if embed else vault.resolve_wikilink(base, source=ctx.doc_path)

    from .vault import IMAGE_EXTENSIONS  # local import keeps module dependency one-way

    if resolved is not None and embed and resolved.path.suffix.lower() in IMAGE_EXTENSIONS:
        token = state.push("image", "img", 0)
        token.attrSet("src", urllib.parse.quote(str(resolved.path)))
        if alias.isdigit():
            token.attrSet("width", alias)
        child = Token("text", "", 0)
        child.content = "" if alias.isdigit() else (alias or resolved.name)
        token.children = [child]
    elif resolved is not None and resolved.path.suffix.lower() in LOCAL_DOCUMENT_EXTENSIONS:
        token = state.push("link_open", "a", 1)
        href = ctx.view_url(resolved.path)
        if heading and not embed:
            href += "#" + urllib.parse.quote(heading.strip())
        token.attrSet("href", href)
        token.attrSet("class", "askw-wikilink askw-embed" if embed else "askw-wikilink")
        token.attrSet("title", resolved.rel)
        token.meta["askw_local"] = True
        text = state.push("text", "", 0)
        text.content = label if (alias or not embed) else resolved.name
        state.push("link_close", "a", -1)
    else:
        token = state.push("wikilink_missing_open", "span", 1)
        token.attrSet("class", "askw-wikilink-missing")
        token.attrSet(
            "title",
            "Unsupported embed" if resolved is not None else f"No note named “{base or target}”",
        )
        text = state.push("text", "", 0)
        text.content = label
        state.push("wikilink_missing_close", "span", -1)
    state.pos = end + 2
    return True


def _render_link_open(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    ctx = (env or {}).get("askw") if env is not None else None
    href = token.attrGet("href")
    if isinstance(href, str) and ctx is not None and not token.meta.get("askw_local"):
        href = ctx.rewrite_href(href)
        token.attrSet("href", href)
    token.attrSet("rel", "noreferrer noopener")
    if isinstance(href, str) and href.lower().startswith(("http://", "https://")):
        token.attrSet("target", "_top")  # leave the reader frame; a no-op at top level
    return self.renderToken(tokens, idx, options, env)


# MARK: - Markdown: Obsidian's inline extras (==highlight==, - [ ] tasks)

def _highlight_rule(state: Any, silent: bool) -> bool:
    """``==highlight==``, as Obsidian writes it: markdown-it's own ``~~strikethrough~~`` rule with ``=`` for ``~``."""
    start = state.pos
    if silent or state.src[start] != "=":
        return False
    scanned = state.scanDelims(start, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        token = state.push("text", "", 0)
        token.content = "="
        length -= 1
    for _ in range(0, length, 2):
        token = state.push("text", "", 0)
        token.content = "=="
        state.delimiters.append(Delimiter(
            marker=0x3D, length=0, token=len(state.tokens) - 1, end=-1,
            open=scanned.can_open, close=scanned.can_close,
        ))
    state.pos += scanned.length
    return True


def _pair_highlights(state: Any, delimiters: list) -> None:
    lone = []
    for start in delimiters:
        if start.marker != 0x3D or start.end == -1:
            continue
        end = delimiters[start.end]
        for index, kind, nesting in ((start.token, "mark_open", 1), (end.token, "mark_close", -1)):
            token = state.tokens[index]
            token.type, token.tag, token.nesting, token.markup, token.content = kind, "mark", nesting, "==", ""
        before = state.tokens[end.token - 1]
        if before.type == "text" and before.content == "=":
            lone.append(end.token - 1)
    # An odd run (`===`) leaves one `=` before its pair; it belongs after the closing tag, as with `~`.
    while lone:
        i = lone.pop()
        j = i + 1
        while j < len(state.tokens) and state.tokens[j].type == "mark_close":
            j += 1
        j -= 1
        if i != j:
            state.tokens[i], state.tokens[j] = state.tokens[j], state.tokens[i]


def _highlight_post(state: Any) -> None:
    _pair_highlights(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _pair_highlights(state, meta["delimiters"])


_TASK_RE = re.compile(r"\[([ xX])\][ \t]")


def _task_lists(state: Any) -> None:
    """``- [ ] task`` and ``- [x] done``: a list item whose text opens with a box is a task, drawn with a checkbox.

    The box carries its source line (``data-askw-line``, counted in the file, frontmatter included) so a click on the
    page can tick that line (``set_task``); only a note's own render knows the line, so only it gets one.
    """
    env = state.env if isinstance(state.env, dict) else {}
    offset = env.get("askw_line_offset")
    tokens = state.tokens
    for i in range(2, len(tokens)):
        inline = tokens[i]
        if inline.type != "inline" or tokens[i - 1].type != "paragraph_open" or tokens[i - 2].type != "list_item_open":
            continue
        found = _TASK_RE.match(inline.content)
        children = inline.children or []
        if not found or not children or children[0].type != "text" or not children[0].content.startswith(found.group(0)):
            continue
        done = found.group(1) != " "
        children[0].content = children[0].content[len(found.group(0)):]
        box = Token("askw_task", "input", 0)
        box.meta = {"done": done}
        if offset is not None and inline.map:
            box.meta["line"] = inline.map[0] + offset
        label = Token("askw_task_label_open", "span", 1)
        label.attrSet("class", "askw-task-label")
        inline.children = [box, label, *children, Token("askw_task_label_close", "span", -1)]
        tokens[i - 2].attrJoin("class", "askw-task is-done" if done else "askw-task")


def _render_task(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    meta = tokens[idx].meta
    line = f' data-askw-line="{int(meta["line"])}"' if "line" in meta else ""
    return f'<input type="checkbox" class="askw-task-box"{" checked" if meta.get("done") else ""}{line} aria-label="Task">'


# MARK: - Markdown: Obsidian's callouts, comments, tags, bare URLs and line breaks

_CALLOUT_RE = re.compile(r"\[!([^\]\n]+)\]([+-]?)[ \t]*(.*)")
# Obsidian's built-in types and their aliases, each with its default colour and Lucide icon. An unknown type is
# drawn as a note, as Obsidian draws it.
_CALLOUT_ALIASES = {
    "summary": "abstract", "tldr": "abstract", "hint": "tip", "important": "tip", "check": "success",
    "done": "success", "help": "question", "faq": "question", "caution": "warning", "attention": "warning",
    "fail": "failure", "missing": "failure", "error": "danger", "cite": "quote",
}
_CALLOUT_ICONS = {
    "note": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
    "abstract": '<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "todo": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "tip": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "success": '<path d="M20 6 9 17l-5-5"/>',
    "question": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "warning": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "failure": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "danger": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "bug": '<path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/><path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/><path d="M6.53 9C4.6 8.8 3 7.1 3 5"/><path d="M6 13H2"/><path d="M3 21c0-2.1 1.7-3.9 3.8-4"/><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/><path d="M22 13h-4"/><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/>',
    "example": '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
    "quote": '<path d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/><path d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/>',
}
_SVG = '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{}</svg>'
_FOLD_ICON = '<span class="callout-fold" aria-hidden="true">' + _SVG.format('<path d="m9 18 6-6-6-6"/>') + "</span>"


# Obsidian's default callout colours, light then dark, by the canonical type each alias draws as.
_CALLOUT_COLOURS = {
    "blue": ("8 109 221", "2 122 255", ("note", "info", "todo")),
    "cyan": ("0 191 188", "83 223 221", ("abstract", "tip")),
    "green": ("8 185 78", "68 207 110", ("success",)),
    "orange": ("236 117 0", "233 151 63", ("question", "warning")),
    "red": ("233 49 71", "251 70 76", ("failure", "danger", "bug")),
    "purple": ("120 82 238", "168 130 255", ("example",)),
    "gray": ("158 158 158", "158 158 158", ("quote",)),
}


def _callout_css() -> str:
    light = ";".join(f"--callout-{name}:{rgb}" for name, (rgb, _, _) in _CALLOUT_COLOURS.items())
    dark = ";".join(f"--callout-{name}:{rgb}" for name, (_, rgb, _) in _CALLOUT_COLOURS.items())
    rules = [
        f":root{{{light}}} @media(prefers-color-scheme:dark){{:root{{{dark}}}}}",
        ".callout{--callout-color:var(--callout-blue);margin:1em 0;padding:12px 12px 12px 24px;border-radius:4px;"
        "background:rgb(var(--callout-color)/.1);overflow:hidden}",
        ".callout-title{display:flex;gap:6px;align-items:flex-start;color:rgb(var(--callout-color));font-weight:600;line-height:1.35}",
        ".callout-icon,.callout-fold{display:flex;flex:0 0 auto;align-items:center;height:1.35em}",
        "summary.callout-title{cursor:pointer;list-style:none} summary.callout-title::-webkit-details-marker{display:none}",
        ".callout-fold{opacity:.7;transition:transform .1s} details.callout[open]>summary .callout-fold{transform:rotate(90deg)}",
        ".callout-content>:first-child{margin-top:.6em} .callout-content>:last-child{margin-bottom:0} .callout-content:empty{display:none}",
    ]
    for name, (_, _, kinds) in _CALLOUT_COLOURS.items():
        types = [kind for kind in kinds] + [alias for alias, kind in _CALLOUT_ALIASES.items() if kind in kinds]
        rules.append(".callout:is(" + ",".join(f'[data-callout="{t}"]' for t in types) + f"){{--callout-color:var(--callout-{name})}}")
    return "\n".join(rules)


_CALLOUT_CSS = _callout_css()


def _callouts(state: Any) -> None:
    """``> [!info] Title``: a blockquote whose first line names a type is Obsidian's callout, ``[!faq]-`` folded shut
    and ``[!faq]+`` foldable but open. Runs before ``inline``, so the title is parsed as Markdown like the body."""
    tokens, out, stack = state.tokens, [], []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "blockquote_close":
            meta = stack.pop()
            if meta is not None:
                close = Token("askw_callout_close", "div", -1)
                close.meta, close.block = meta, True
                out.append(close)
                i += 1
                continue
        if token.type != "blockquote_open":
            out.append(token)
            i += 1
            continue
        found = None
        if i + 2 < len(tokens) and tokens[i + 1].type == "paragraph_open" and tokens[i + 2].type == "inline":
            first, _, rest = tokens[i + 2].content.partition("\n")
            found = _CALLOUT_RE.fullmatch(first.strip())
        if not found:
            stack.append(None)
            out.append(token)
            i += 1
            continue
        raw = found.group(1).strip()
        kind = _CALLOUT_ALIASES.get(raw.lower(), raw.lower())
        meta = {
            "type": re.sub(r"[^a-z0-9_-]+", "-", raw.lower()),
            "icon": kind if kind in _CALLOUT_ICONS else "note",
            "fold": found.group(2),
        }
        stack.append(meta)
        opening = Token("askw_callout_open", "div", 1)
        opening.meta, opening.block, opening.map = meta, True, token.map
        title = Token("inline", "", 0)
        title.content, title.map, title.children = found.group(3).strip() or raw[:1].upper() + raw[1:], token.map, []
        out += [opening, Token("askw_callout_title_open", "div", 1), title, Token("askw_callout_title_close", "div", -1)]
        out[-3].meta = out[-1].meta = meta
        content = Token("askw_callout_content_open", "div", 1)
        content.attrSet("class", "callout-content")
        content.block = True
        out.append(content)
        if rest.strip():
            tokens[i + 2].content = rest
            i += 1  # keep the paragraph, minus its first line
        else:
            i += 4  # the first line was the whole paragraph
    state.tokens[:] = out


def _render_callout_open(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    meta = tokens[idx].meta
    if not meta["fold"]:
        return f'<div class="callout" data-callout="{meta["type"]}">\n'
    shut = meta["fold"] == "-"
    return f'<details class="callout is-collapsible{" is-collapsed" if shut else ""}" data-callout="{meta["type"]}"{"" if shut else " open"}>\n'


def _render_callout_title_open(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    meta = tokens[idx].meta
    tag = "summary" if meta["fold"] else "div"
    icon = _SVG.format(_CALLOUT_ICONS[meta["icon"]])
    return f'<{tag} class="callout-title"><span class="callout-icon" aria-hidden="true">{icon}</span><span class="callout-title-inner">'


def _render_callout_title_close(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    fold = tokens[idx].meta["fold"]
    return "</span>" + (_FOLD_ICON + "</summary>\n" if fold else "</div>\n")


def _render_callout_close(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    return "</div>\n" + ("</details>\n" if tokens[idx].meta["fold"] else "</div>\n")


def _comment_block(state: Any, start: int, end: int, silent: bool) -> bool:
    """A ``%%`` comment that opens a line hides everything up to the ``%%`` that closes it, blank lines included, or
    to the end of the note when nothing does, as in Obsidian. One that closes mid-line is the inline rule's."""
    if state.sCount[start] - state.blkIndent >= 4:
        return False
    pos, line_end = state.bMarks[start] + state.tShift[start], state.eMarks[start]
    if not state.src.startswith("%%", pos):
        return False
    line, close = start, state.src.find("%%", pos + 2, line_end)
    while close < 0 and line + 1 < end:
        line += 1
        close = state.src.find("%%", state.bMarks[line] + state.tShift[line], state.eMarks[line])
    if close >= 0 and state.src[close + 2 : state.eMarks[line]].strip():
        return False
    if not silent:
        state.line = line + 1
    return True


def _comment_inline(state: Any, silent: bool) -> bool:
    if not state.src.startswith("%%", state.pos):
        return False
    close = state.src.find("%%", state.pos + 2, state.posMax)
    if close < 0:
        return False
    state.pos = close + 2  # hidden: nothing is pushed
    return True


_TAG_RE = re.compile(r"#([^\W][\w/-]*)")


def _tag_rule(state: Any, silent: bool) -> bool:
    """``#tag`` and ``#nested/tag`` in a vault note, drawn as the Properties box draws a note's tags. A tag needs a space
    (or the line's start) before it and a character that isn't a digit, so ``#1`` and ``page#anchor`` stay text."""
    ctx = (state.env or {}).get("askw") if state.env is not None else None
    if ctx is None or ctx.vault is None or state.src[state.pos] != "#":
        return False
    if state.pos > 0 and not state.src[state.pos - 1].isspace():
        return False
    found = _TAG_RE.match(state.src, state.pos, state.posMax)
    if not found or found.group(1).replace("/", "").replace("-", "").replace("_", "").isdigit():
        return False
    if not silent:
        token = state.push("askw_tag_open", "span", 1)
        token.attrSet("class", "askw-tag")
        state.push("text", "", 0).content = found.group(0)
        state.push("askw_tag_close", "span", -1)
    state.pos = found.end()
    return True


_BARE_URL_RE = re.compile(r"(?<![\w/.@])https?://[^\s<>\"'`]+")


def _bare_url_end(url: str) -> str:
    # Trailing punctuation ends the sentence, not the address; a `)` stays when the address opened one (Wikipedia).
    while url and url[-1] in ".,;:!?*_~'\")]}":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _bare_urls(state: Any) -> None:
    """``https://example.com`` written bare is a link, as Obsidian and GitHub draw it. Text inside a link or code is
    left alone; only the http(s) schemes are linked."""
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        children, depth = [], 0
        for child in block.children:
            if child.type == "link_open":
                depth += 1
            elif child.type == "link_close":
                depth -= 1
            if child.type != "text" or depth or "://" not in child.content:
                children.append(child)
                continue
            text, pos = child.content, 0
            for found in _BARE_URL_RE.finditer(text):
                if found.start() < pos:
                    continue
                url = _bare_url_end(found.group(0))
                if not url.split("://", 1)[1]:
                    continue
                if found.start() > pos:
                    children.append(Token("text", "", 0, content=text[pos:found.start()]))
                link = Token("link_open", "a", 1)
                link.attrSet("href", url)
                link.markup, link.info = "linkify", "auto"
                children += [link, Token("text", "", 0, content=url), Token("link_close", "a", -1)]
                pos = found.start() + len(url)
            if pos < len(text):
                children.append(Token("text", "", 0, content=text[pos:]))
        block.children = children


def _render_softbreak(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    # Obsidian draws a vault note's single newline as a line break (its "Strict line breaks" is off by default). Other
    # Markdown keeps CommonMark's soft break, as GitHub draws a README.
    ctx = (env or {}).get("askw") if env is not None else None
    return "<br>\n" if ctx is not None and ctx.vault is not None else "\n"


def _build_markdown() -> MarkdownIt:
    # markdown-it escapes raw HTML and rejects unsafe URL schemes under the
    # CommonMark preset. Tables are the one GitHub-style extension readers need
    # most often; strikethrough, highlights and task lists are Obsidian's everyday
    # extras. Everything remains local and deterministic.
    md = MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
        },
    ).enable(["table", "strikethrough"])
    md.validateLink = _validate_link  # type: ignore[method-assign]
    # Before ``link`` so ``[[Note]]`` is never mistaken for a reference link.
    # ``backticks`` runs earlier, so wikilinks inside code spans stay literal.
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.inline.ruler.after("strikethrough", "askw_highlight", _highlight_rule)
    md.inline.ruler2.after("strikethrough", "askw_highlight", _highlight_post)
    md.inline.ruler.before("backticks", "askw_comment", _comment_inline)
    md.inline.ruler.before("link", "askw_tag", _tag_rule)
    # Before ``fence``, so it can interrupt a paragraph; a ``%%`` inside a code block is code, not a comment.
    md.block.ruler.before("fence", "askw_comment", _comment_block, {"alt": ["paragraph", "reference", "blockquote", "list"]})
    md.core.ruler.before("inline", "askw_callouts", _callouts)
    # After ``text_join``, which merges the ``[`` and `` ]`` of a box into the text they open.
    md.core.ruler.push("askw_tasks", _task_lists)
    md.core.ruler.push("askw_bare_urls", _bare_urls)
    md.add_render_rule("link_open", _render_link_open)
    md.add_render_rule("askw_task", _render_task)
    md.add_render_rule("softbreak", _render_softbreak)
    md.add_render_rule("askw_callout_open", _render_callout_open)
    md.add_render_rule("askw_callout_title_open", _render_callout_title_open)
    md.add_render_rule("askw_callout_title_close", _render_callout_title_close)
    md.add_render_rule("askw_callout_close", _render_callout_close)
    return md


_MARKDOWN = _build_markdown()


def image_asset(target: str, kind: str, ctx: RenderContext, *, src: str, fs_prefix: str) -> tuple[str, str | None] | None:
    """The URL the reader would draw an image in the editor with, and the file it reads (None for an outside address).

    Worked out as the page works it out: the renderer turns ``![](target)`` or ``![[target]]`` into an ``<img>``, and
    ``prepare_html``'s rewrite turns its source into a ``/_fs`` URL. None when the reference draws no image at all.
    The caller still has to check the file is one the saved note references before it may be served.
    """
    if "\n" in target or (kind == "wiki" and "]]" in target):
        return None
    markup = f"![[{target}]]" if kind == "wiki" else f"![]({target})"
    found = re.search(r'<img src="([^"]*)"', _MARKDOWN.renderInline(markup, {"askw": ctx}))
    if not found:
        return None
    ref = _html.unescape(found.group(1)).strip()
    if ref.lower().startswith(("http://", "https://", "data:image/")):
        return ref, None
    if not ref or ref.lower().startswith(_SKIP_PREFIXES):
        return None
    sink: set[str] = set()
    url = _rewrite_asset(ref, remote=False, base="", doc_dir=Path(src).resolve().parent, fs_prefix=fs_prefix, sink=sink)
    asset = next(iter(sink), None)
    if asset is None or not ASSET_CONTENT_TYPES.get(Path(asset).suffix.lower(), "").startswith("image/"):
        return None
    return url, asset


def link_href(target: str, kind: str, ctx: RenderContext) -> str | None:
    """Where a link the editor was clicked on leads: the reader's URL for it, worked out by the renderer that draws the
    same link on the page (``kind`` is ``"wiki"`` for ``[[target]]``, else a Markdown link's destination). None when it
    leads to no local page — a missing note, or an outside address, which the editor opens itself."""
    if "\n" in target or (kind == "wiki" and "]]" in target):
        return None
    markup = f"[[{target}]]" if kind == "wiki" else f"[x]({target})"
    found = re.search(r'<a href="([^"]*)"', _MARKDOWN.renderInline(markup, {"askw": ctx}))
    href = _html.unescape(found.group(1)) if found else ""
    return href if href.startswith("/view?") else None


def _markdown_html(raw: str, title: str, *, ctx: RenderContext) -> str:
    block, body = split_frontmatter(raw)
    # The body's first line is this many lines into the file (the frontmatter's), for the task boxes' source lines.
    env: dict[str, Any] = {"askw": ctx, "askw_line_offset": raw[: len(raw) - len(body)].count("\n")}
    properties = _render_properties(block, _MARKDOWN, env) if block is not None else ""
    rendered = _MARKDOWN.render(body, env)
    return _reading_shell(title, properties + rendered, kind="markdown")


def _reading_shell(title: str, body: str, *, kind: str) -> str:
    # ``main`` is relative so WebKit takes it for a selection root: a selection across blocks fills the gaps
    # between them out to the root's edges, which were the window's while the root was ``body``.
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{_html.escape(title)}</title>
<style>
:root{{color-scheme:light dark;--reader-bg:247 247 247;--reader-pane:255 255 255;--reader-ink:38 36 33;--reader-muted:87 83 78;--reader-faint:168 162 158;--reader-line:0 0 0;--reader-code:243 242 239;--reader-accent:58 131 247}}
html,body{{min-height:100%;background:transparent}} body{{margin:0;background:rgb(var(--reader-bg)/.76);color:rgb(var(--reader-ink));font:17px/1.72 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;backdrop-filter:saturate(1.08)}}
main{{position:relative;box-sizing:border-box;max-width:860px;min-height:100vh;margin:0 auto;padding:64px 72px 110px;background:rgb(var(--reader-pane)/.86);border-inline:1px solid rgb(var(--reader-line)/.09);box-shadow:0 18px 55px rgb(0 0 0/.06);backdrop-filter:blur(22px) saturate(1.16)}}
h1,h2,h3{{line-height:1.2;letter-spacing:-.02em}} h1{{font-size:2.35rem}} h2{{margin-top:2.2em}}
pre{{overflow:auto;padding:18px;border-radius:10px;background:rgb(var(--reader-code)/.88);font:14px/1.55 ui-monospace,SFMono-Regular,monospace}}
code{{background:rgb(var(--reader-code)/.88);padding:.12em .32em;border-radius:4px}} pre code{{padding:0}}
a{{color:rgb(var(--reader-accent))}} blockquote{{margin-left:0;padding-left:20px;border-left:3px solid rgb(var(--reader-line)/.16);color:rgb(var(--reader-muted))}}
table{{width:100%;margin:1.4em 0;border-collapse:collapse;font-size:.92em}} th,td{{padding:9px 11px;border:1px solid rgb(var(--reader-line)/.14);text-align:left;vertical-align:top}} th{{background:rgb(var(--reader-code)/.72);font-weight:650}} tbody tr:nth-child(even){{background:rgb(var(--reader-code)/.34)}}
hr{{border:0;border-top:1px solid rgb(var(--reader-line)/.14);margin:2em 0}} img{{max-width:100%;height:auto}}
.askw-pdf-page{{position:relative;margin:0 0 32px;padding:36px 44px;border:1px solid rgb(var(--reader-line)/.11);background:rgb(var(--reader-pane)/.64);box-shadow:0 8px 24px rgb(0 0 0/.07);backdrop-filter:blur(12px)}}
.askw-page-label{{margin:0 0 24px;color:rgb(var(--reader-faint));font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}}
.askw-properties{{margin:0 0 1.6em;padding:9px 14px;border:1px solid rgb(var(--reader-line)/.12);border-radius:10px;background:rgb(var(--reader-code)/.5);font-size:.88em}} .askw-properties summary{{cursor:pointer;color:rgb(var(--reader-muted));font-weight:600;letter-spacing:.02em}}
.askw-properties dl{{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 18px;margin:10px 0 2px}} .askw-properties dt{{color:rgb(var(--reader-muted));font-weight:600}} .askw-properties dd{{margin:0;overflow-wrap:anywhere}} .askw-properties pre{{margin:8px 0 0}} .askw-empty{{color:rgb(var(--reader-faint))}}
li.askw-task{{list-style:none}} .askw-task-box{{margin:0 .55em 0 -1.45em;vertical-align:-.1em;cursor:pointer}} li.askw-task.is-done>.askw-task-label,li.askw-task.is-done>p>.askw-task-label{{color:rgb(var(--reader-faint));text-decoration:line-through}}
mark{{padding:0 .1em;border-radius:3px;background:rgb(255 208 0/.4);color:inherit}}
.askw-tag{{display:inline-block;margin:0 4px 2px 0;padding:1px 8px;border-radius:999px;background:rgb(var(--reader-accent)/.12);color:rgb(var(--reader-accent));font-size:.85em}} main :is(p,li,td,th,.callout-title-inner)>.askw-tag{{margin:0}}
.askw-wikilink-missing{{border-bottom:1px dotted rgb(var(--reader-faint));color:rgb(var(--reader-muted));cursor:help}} a.askw-embed{{display:inline-block;padding:1px 8px;border:1px dashed rgb(var(--reader-line)/.25);border-radius:6px;text-decoration:none}} a.askw-embed::before{{content:"⧉ ";opacity:.6}}
{_CALLOUT_CSS}
@media(prefers-color-scheme:dark){{:root{{--reader-bg:24 24 24;--reader-pane:31 31 31;--reader-ink:245 245 245;--reader-muted:205 205 205;--reader-faint:143 143 143;--reader-line:255 255 255;--reader-code:48 48 48}} body{{background:rgb(var(--reader-bg)/.70)}} main{{background:rgb(var(--reader-pane)/.78);box-shadow:0 18px 60px rgb(0 0 0/.28)}}}}
@media(max-width:720px){{main{{padding:36px 24px}}}}
@media(prefers-reduced-transparency:reduce){{body,main,.askw-pdf-page{{backdrop-filter:none}} body{{background:rgb(var(--reader-bg))}} main{{background:rgb(var(--reader-pane))}}}}
</style></head><body data-askw-document-kind="{kind}"><main>{body}</main></body></html>"""


def _pdf_html(path: Path) -> LoadedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise ViewerError("PDF support is unavailable in this build.") from exc
    try:
        reader = PdfReader(str(path))
        title = str((reader.metadata or {}).get("/Title") or path.stem)
        pages: list[str] = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            paragraphs = "".join(
                f"<p>{_html.escape(chunk.strip())}</p>"
                for chunk in re.split(r"\n\s*\n", text)
                if chunk.strip()
            )
            if not paragraphs:
                paragraphs = "<p><em>No extractable text was found on this page.</em></p>"
            pages.append(
                f'<section class="askw-pdf-page" data-askw-page="{number}">'
                f'<p class="askw-page-label">Page {number}</p>{paragraphs}</section>'
            )
    except Exception as exc:
        raise ViewerError(f"Could not read PDF {path}: {exc}") from exc
    return LoadedDocument(
        html=_reading_shell(title, "".join(pages), kind="pdf"),
        title=title,
        kind="pdf",
        page_count=len(reader.pages),
    )


def load_local_document(
    path: Path,
    *,
    display_path: Path | None = None,
    folder: str | None = None,
    vault: "VaultIndex | None" = None,
) -> LoadedDocument:
    """Load a local document for the reader.

    ``path`` is read from disk; ``display_path`` (default ``path``) is the lexical
    path relative links and wikilinks resolve against — they differ for a note
    under a symlinked vault folder.
    """
    suffix = path.suffix.lower()
    if suffix not in LOCAL_DOCUMENT_EXTENSIONS:
        raise ViewerError("Supported local documents: HTML, Markdown, text, and PDF.")
    if suffix == ".pdf":
        return _pdf_html(path)
    raw = read_local(path)
    if suffix in {".html", ".htm"}:
        match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        title = _html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else path.stem
        return LoadedDocument(raw, title or path.stem, "html")
    if suffix in {".md", ".markdown"}:
        block, body = split_frontmatter(raw)
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = heading.group(1).strip() if heading else ""
        if not title and block:
            fields = parse_flat_frontmatter(block) or {}
            front_title = fields.get("title")
            title = front_title.strip() if isinstance(front_title, str) else ""
        title = title or path.stem
        ctx = RenderContext(doc_path=display_path or path, folder=folder, vault=vault)
        return LoadedDocument(_markdown_html(raw, title, ctx=ctx), title, "markdown")
    title = path.stem
    body = f"<h1>{_html.escape(title)}</h1><pre>{_html.escape(raw)}</pre>"
    return LoadedDocument(_reading_shell(title, body, kind="text"), title, "text")


def _rewrite_asset(
    ref: str, *, remote: bool, base: str, doc_dir: Path | None, fs_prefix: str, sink: set[str]
) -> str:
    ref = ref.strip()
    if not ref or ref.lower().startswith(_SKIP_PREFIXES):
        return ref
    if remote:
        return urllib.parse.urljoin(base, ref)
    # Local: resolve against the document's on-disk directory, register the exact
    # file in `sink` (the /_fs allowlist), and rewrite to a /_fs URL.
    assert doc_dir is not None
    target = (doc_dir / urllib.parse.unquote(ref)).resolve()
    sink.add(str(target))
    return fs_prefix + urllib.parse.quote(str(target))


def prepare_html(
    src: str,
    *,
    html_text: str,
    server_origin: str,
    folder: str | None,
    asset_token: str | None = None,
    allow_document_scripts: bool = False,
) -> tuple[str, set[str]]:
    """Rewrite ``html_text`` so the widget can run against it under ``server_origin``.

    Returns ``(html, local_assets)`` — ``local_assets`` is the exact set of on-disk
    files this (local) document references, to be added to the /_fs allowlist. It is
    empty for remote documents (whose assets stay absolute on the original site).
    """
    remote = is_remote(src)
    fs_prefix = f"{server_origin}/_fs"
    if asset_token:
        fs_prefix += f"/{asset_token}"
    assets: set[str] = set()

    if remote:
        embedded = _EMBEDDED_BASE_HREF_RE.search(html_text)
        base = urllib.parse.urljoin(src, embedded.group(1)) if embedded else src
        doc_dir: Path | None = None
    else:
        base = ""
        doc_dir = Path(src).expanduser().resolve().parent

    def _sub(m: re.Match) -> str:
        # group(1)=prefix incl. opening quote, group(2)=quote char, group(3)=the URL
        rewritten = _rewrite_asset(
            m.group(3), remote=remote, base=base, doc_dir=doc_dir, fs_prefix=fs_prefix, sink=assets
        )
        return m.group(1) + rewritten + m.group(2)

    out = _ASSET_RE.sub(_sub, html_text)

    # Remote and untrusted documents stay inert. The /view route opts trusted local
    # HTML into scripts so buttons, tabs, diagrams, and other authored interactions
    # continue to work. Script assets were already rewritten through the exact-file
    # capability above; remote documents can never opt in.
    if remote or not allow_document_scripts:
        out = _SCRIPT_RE.sub("", out)
    # Drop <base> (so in-page #anchors resolve against the /view URL) and any
    # page-set CSP (we set our own, stricter one as a response header).
    out = _BASE_RE.sub("", out)
    out = _CSP_RE.sub("", out)

    # Seed the widget's folder via a <meta>. The widget reads it on boot.
    seed = ""
    if folder:
        seed = f'<meta name="askw-folder" content="{_html.escape(folder, quote=True)}">'
    # Seed the source path for local docs so the widget can poll /_mtime and
    # live-reload when an external editor (e.g. another agent) rewrites the file.
    # Remote docs have no mtime, so they get no seed and never poll.
    if not remote:
        seed += f'<meta name="askw-src" content="{_html.escape(src, quote=True)}">'
    if asset_token:
        seed += f'<meta name="askw-doc-token" content="{_html.escape(asset_token, quote=True)}">'
    inject = f'{seed}<script src="{server_origin}/ask.js"></script>'

    if _BODY_RE.search(out):
        out = _BODY_RE.sub(inject + "</body>", out, count=1)
    else:
        out += inject
    return out, assets


def resolve_fs_path(path: str, *, allowed: set[str], home: Path) -> tuple[Path, str] | None:
    """Validate a /_fs request. Returns (abspath, content_type) or None if refused.

    /_fs is locked to the **exact files a viewed local document referenced**
    (``allowed``) — it is not a general home-directory reader. A drive-by page
    opened via /view (remote ``src``) registers no local assets, so it cannot pull
    ``~/.claude.json`` etc. The home check + extension allowlist are extra floors.
    """
    try:
        resolved = Path("/" + path.lstrip("/")).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if str(resolved) not in allowed:
        return None
    if not resolved.is_file():
        return None
    ctype = ASSET_CONTENT_TYPES.get(resolved.suffix.lower())
    if ctype is None:
        return None
    try:
        if not (resolved == home or resolved.is_relative_to(home)):
            return None
    except ValueError:
        return None
    return resolved, ctype

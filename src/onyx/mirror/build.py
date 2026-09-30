"""Build the pages and assets a phone mirror publishes, as plaintext objects.

This is the half of the mirror that decides *what* leaves the Mac. It opens no socket and holds no key: it reads the
vaults through Onyx's own index, renders each page the way the reader does, and hands back objects named as the wire
format names them (``docs/plans/phone-mirror.md``, "Wire format v1"), for the publisher to encrypt and send.

Two gates live here, and each is a boundary, not a preference:

* **Membership (gate 3).** A page is published only if ``VaultIndex`` lists it (the sidebar's own list, so the mirror
  follows exactly the links the vault already follows and never walks the disk itself), its mirror path sits under an
  ``include`` entry and not under an ``exclude`` one, and nothing on the way is a dot-folder or a ``mirror: false`` note.
* **Assets (gate 4).** A file is uploaded only if a *published* page references it, and only a type ``/_fs`` would
  serve, from inside the home folder as ``/_fs`` requires. A folder is never uploaded wholesale, and a file referenced
  only by a page that stayed out stays out too.

A link to a published page becomes that page's id, to one that isn't ``#onyx-unpublished``, so the page never carries
a path into a note the mirror left behind.
"""

from __future__ import annotations

import html as _html
import os
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import markdown_theme, vault, viewer

NOTES = "Notes"
ARTIFACTS = "Artifacts"
UNPUBLISHED = "#onyx-unpublished"
MISSING = "#onyx-missing"
MAX_TEXT_CHARS = 20_000
PAGE_MIME = "text/html; charset=utf-8"
# Never an asset, whatever ASSET_CONTENT_TYPES grows to hold: a .json beside a page is as likely to be config with
# secrets in it as data, which is why /_fs refuses it.
_NEVER_ASSETS = {".json", ".map"}
_MIRROR_OFF = {"false", "no", "off"}


@dataclass(frozen=True)
class BuiltObject:
    name: str  # "page:<mirror path>" or "asset:<realpath>"
    id: str  # ids(name)
    data: bytes  # plaintext
    mime: str
    page: dict | None  # pages: {"path", "title", "kind", "mtime", "text"}; assets: None


class MirrorBuild(list[BuiltObject]):
    """What ``build_mirror`` returns: the objects, plus what it could not build.

    It is a plain list to a caller that wants only the objects. A publisher that also reads ``skipped`` can keep the
    last published copy of those pages: left to the index alone, a file that failed to read once is dropped from it, and
    its old object is deleted with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.skipped: list[str] = []  # mirror paths of pages left out because reading or rendering them failed
        self.truncated = False  # a vault index hit its entry cap, so pages past it were never seen


# MARK: - Membership


def _key(part: str) -> str:
    # macOS volumes fold case and may store a name decomposed. An include that missed on either would publish less
    # than was asked, but an exclude that missed would publish what it names, so both sides are compared folded.
    return unicodedata.normalize("NFC", part).casefold()


def _entries(paths: list[str]) -> list[tuple[str, ...]]:
    """``include``/``exclude`` entries as folded path components. An empty entry names nothing, never "everything"."""
    entries = []
    for path in paths:
        parts = tuple(_key(part) for part in str(path).split("/") if part not in ("", "."))
        if parts:
            entries.append(parts)
    return entries


def _under(parts: tuple[str, ...], entry: tuple[str, ...]) -> bool:
    # Components, not a string prefix, so "Notes/Area" does not reach "Notes/Areas".
    return parts[: len(entry)] == entry


@dataclass
class _Candidate:
    mirror: str  # "Notes/<rel>" or "Artifacts/<entry_rel>"
    item: vault.VaultFile
    index: vault.VaultIndex
    artifact: bool

    def listed(self, includes: list[tuple[str, ...]], excludes: list[tuple[str, ...]]) -> bool:
        paths = [self.mirror.split("/")]
        if self.artifact and self.item.page_dir:
            # A guide folder's mirror path stops at the folder; an exclude naming the index.html inside must still hold.
            paths.append([ARTIFACTS, *self.item.rel.split("/")])
        if any(part.startswith(".") for names in paths for part in names):
            return False
        folded = [tuple(_key(part) for part in names) for names in paths]
        if not any(_under(folded[0], entry) for entry in includes):
            return False
        return not any(_under(names, entry) for names in folded for entry in excludes)


_MIRROR_LINE_RE = re.compile(r"""^mirror[ \t]*:[ \t]*["']?(\w+)["']?[ \t]*(?:#.*)?$""", re.IGNORECASE | re.MULTILINE)


def _opted_out(raw: str) -> bool:
    """A note whose frontmatter says ``mirror: false`` (or ``no``/``off``, quoted or not) asks to stay on the Mac."""
    # A byte-order mark leaves the fence off offset 0, so the reader shows the frontmatter as plain text. The flag
    # still means what it says, so it is read past the mark.
    text = raw.removeprefix("﻿")
    block, _body = viewer.split_frontmatter(text)
    fields = viewer.parse_flat_frontmatter(block) if block is not None else None
    if fields is not None:
        values = [value for key, value in fields.items() if key.casefold() == "mirror"]
    else:
        # Nested YAML the flat parser gives up on, or a block that isn't YAML-shaped: a top-level ``mirror:`` line of
        # its own still counts, so a flag never stops working because the note's other properties got elaborate.
        if block is None:
            fenced = viewer._FRONTMATTER_RE.match(text)
            block = (fenced.group(1) or "") if fenced else ""
        values = _MIRROR_LINE_RE.findall(block)
    return any(isinstance(value, str) and value.strip().casefold() in _MIRROR_OFF for value in values)


# MARK: - Links and assets


@dataclass
class _Pages:
    """Where a link can land: every page being published, by the path the vault shows it at and by its real file."""

    lexical: dict[str, str]
    real: dict[str, str]


class _Assets:
    """The files pages reference, each read once however many pages embed it."""

    def __init__(self, ids: Callable[[str], str], home: Path) -> None:
        self._ids = ids
        self._home = home  # a realpath, so it is compared with the realpath of the asset
        self._loaded: dict[str, BuiltObject | None] = {}

    def get(self, real: Path) -> BuiltObject | None:
        key = str(real)
        if key not in self._loaded:
            self._loaded[key] = self._load(real)
        return self._loaded[key]

    def _load(self, real: Path) -> BuiltObject | None:
        suffix = real.suffix.lower()
        mime = viewer.ASSET_CONTENT_TYPES.get(suffix)
        if mime is None or suffix in _NEVER_ASSETS or mime == "application/json":
            return None
        # The floor /_fs puts under every file it serves (``resolve_fs_path``): a page can name a path anywhere, and
        # ``real`` is already past every link, so a link out of the home folder does not get a file across it.
        if real != self._home and not real.is_relative_to(self._home):
            return None
        try:
            if not real.is_file():
                return None
            data = real.read_bytes()
        except OSError:
            return None
        name = f"asset:{real}"
        return BuiltObject(name=name, id=self._ids(name), data=data, mime=mime, page=None)


def _split(ref: str) -> urllib.parse.SplitResult | None:
    """``ref`` taken apart, if it names something on this Mac: not a remote URL, an in-page anchor, a mailto:, or
    another app's scheme. ``urlsplit``, not ``urlparse``, which would cut a ``;`` in a filename off as parameters."""
    if not ref or ref.lower().startswith(viewer._SKIP_PREFIXES):
        return None
    try:
        parts = urllib.parse.urlsplit(ref)
    except ValueError:
        return None
    if (parts.scheme and parts.scheme.lower() != "file") or not parts.path:
        return None
    return parts


def _resolve(base_dir: Path, path: str) -> Path | None:
    # As prepare_html resolves an asset: against the document's real folder, then through every link on the way.
    try:
        return (base_dir / urllib.parse.unquote(path)).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""(?<![\w-])href=(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_TITLE_ATTR_RE = re.compile(r"""\s+title=(["']).*?\1""", re.IGNORECASE | re.DOTALL)
# A script's or style's body is code, not markup: a template string such as '<a href="' + url + '">' holds nothing to
# rewrite, and rewriting it would turn a link the page builds itself into the placeholder.
_RAW_TEXT_RE = re.compile(r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.IGNORECASE | re.DOTALL)


def _in_markup(html: str, rewrite: Callable[[str], str]) -> str:
    """``rewrite`` applied to the markup of ``html``: everything but the bodies of ``<script>`` and ``<style>``.

    An opening ``<script src="…">`` stays in the markup, so its source is still rewritten."""
    out: list[str] = []
    pos = 0
    for found in _RAW_TEXT_RE.finditer(html):
        out.append(rewrite(html[pos : found.end(1)]))
        out.append(found.group(3))
        pos = found.start(4)
    out.append(rewrite(html[pos:]))
    return "".join(out)


class _Linker:
    """Points one page's references at ids, and remembers which assets it reached for."""

    def __init__(self, base_dir: Path, pages: _Pages, assets: _Assets) -> None:
        self.base_dir = base_dir  # the page's real folder, as /view resolves a document's assets
        self.pages = pages
        self.assets = assets
        self.used: dict[str, BuiltObject] = {}

    def _page(self, path: Path) -> str | None:
        found = self.pages.lexical.get(str(path))  # the path as the vault shows it, before any link is followed
        if found is not None:
            return found
        try:
            real = Path(os.path.realpath(path))
            found = self.pages.real.get(str(real))
            if found is None and real.is_dir():  # a link to a guide's folder is a link to its page
                for name in vault.INDEX_NAMES:
                    found = self.pages.real.get(os.path.realpath(real / name))
                    if found is not None:
                        break
        except (OSError, ValueError):
            return None
        return found

    def _asset(self, target: Path) -> str:
        built = self.assets.get(target)
        if built is None:
            return MISSING
        self.used[built.name] = built
        return built.id

    def link(self, ref: str) -> str | None:
        """Where an ``<a href>`` leads in the mirror, or None to leave it as written (a remote URL, an in-page anchor)."""
        parts = _split(ref)
        if parts is None:
            return None
        fragment = f"#{parts.fragment}" if parts.fragment else ""
        if parts.path == "/view" and not parts.scheme and not parts.netloc:
            # What the reader's renderer wrote for a wikilink or a link to a local document. The renderer builds its
            # own context inside ``load_local_document``, so its URL is read back here rather than patched out of it:
            # swapping ``viewer.RenderContext`` for the publisher's thread would change /view's links in the live app.
            src = next((value for key, value in urllib.parse.parse_qsl(parts.query) if key == "src"), None)
            found = self._page(vault.normalize(src)) if src else None
            return found + fragment if found else UNPUBLISHED
        target = _resolve(self.base_dir, parts.path)
        if target is None:
            return UNPUBLISHED
        found = self._page(target)
        if found is not None:
            return found + fragment
        if target.suffix.lower() in viewer.ASSET_CONTENT_TYPES:
            return self._asset(target)
        return UNPUBLISHED

    def anchor(self, tag: re.Match[str]) -> str:
        text = tag.group(0)
        href = _HREF_RE.search(text)
        if href is None:
            return text
        new = self.link(_html.unescape(href.group(2)).strip())
        if new is None:
            return text
        text = f"{text[: href.start()]}href={href.group(1)}{_html.escape(new)}{href.group(1)}{text[href.end() :]}"
        # A wikilink's tooltip is its target's path in the vault, so a note left out would still be named in the page.
        return _TITLE_ATTR_RE.sub("", text) if new == UNPUBLISHED else text

    def asset(self, found: re.Match[str]) -> str:
        parts = _split(_html.unescape(found.group(3)).strip())
        if parts is None:
            return found.group(0)
        target = _resolve(self.base_dir, parts.path)
        new = self._asset(target) if target is not None else MISSING
        if new != MISSING and parts.fragment:
            new += f"#{parts.fragment}"  # an svg sprite's ``#icon`` still has to find its symbol
        return found.group(1) + _html.escape(new) + found.group(2)


# MARK: - Pages


_HEAD_RE = re.compile(r"<head\b.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_DROPPED_RE = re.compile(r"<(script|style)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL)
# Tags that sit inside a word or against punctuation; dropping them outright keeps "see <a>this</a>." from reading
# "see this ." in search.
_INLINE_TAG_RE = re.compile(
    r"</?(?:a|abbr|b|bdi|bdo|cite|code|del|dfn|em|font|i|ins|kbd|mark|q|s|samp|small|span|strong|sub|sup|time|u|var)"
    r"\b[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]*>")


def _plain_text(html: str) -> str:
    """The text of a built page, for search on the phone: no head, scripts or styles, no tags, entities undone."""
    text = _HEAD_RE.sub(" ", html)
    text = _DROPPED_RE.sub(" ", text)
    text = _INLINE_TAG_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    return " ".join(_html.unescape(text).split())[:MAX_TEXT_CHARS]


@dataclass
class _Page:
    cand: _Candidate
    real: Path
    id: str


def _build_page(
    page: _Page, pages: _Pages, assets: _Assets, markdown_css: str | None
) -> tuple[BuiltObject, dict[str, BuiltObject]]:
    item = page.cand.item
    loaded = viewer.load_local_document(
        page.real,
        display_path=item.path,
        vault=None if page.cand.artifact else page.cand.index,
    )
    html = loaded.html
    if markdown_css and loaded.kind in markdown_theme.KINDS:
        # As /view injects it, so the copy reads the way the app does.
        html = html.replace("</head>", f'<style id="askw-markdown-theme">{markdown_css}</style></head>', 1)
    linker = _Linker(page.real.parent, pages, assets)
    html = _in_markup(
        html,
        lambda markup: _ANCHOR_RE.sub(linker.anchor, viewer._ASSET_RE.sub(linker.asset, viewer._BASE_RE.sub("", markup))),
    )
    name = f"page:{page.cand.mirror}"
    meta: dict[str, Any] = {
        "path": page.cand.mirror,
        # A page in Artifacts is named as the sidebar names it: its <title>, else its folder. Neither is what
        # load_local_document falls back to for a guide's index.html.
        "title": item.label if page.cand.artifact else loaded.title,
        "kind": "text" if loaded.kind == "pdf" else loaded.kind,
        "mtime": os.stat(page.real).st_mtime,
        "text": _plain_text(html),
    }
    built = BuiltObject(name=name, id=page.id, data=html.encode("utf-8"), mime=PAGE_MIME, page=meta)
    return built, linker.used


def build_mirror(
    *,
    roots: dict[str, Path],
    include: list[str],
    exclude: list[str],
    ids: Callable[[str], str],
    markdown_css: str | None = None,
    home: Path | None = None,
) -> list[BuiltObject]:
    """Every page the mirror publishes and every asset those pages reference, as plaintext objects.

    ``roots`` maps ``"Notes"`` and ``"Artifacts"`` to their folders, either of which may be absent; a page's mirror path
    is ``Notes/<path in the notes index>`` or ``Artifacts/<its entry in Artifacts>``. ``include`` names what to publish
    and there is no default: an empty list publishes nothing, and a root no entry reaches is not even walked.

    An asset is uploaded only from inside ``home`` (default: the user's home folder), compared by path components after
    every link is followed; one outside it is left as ``#onyx-missing``, as a file that isn't there is. Pages are not held
    to it: they are named by the include list, which is the owner's own say.

    One file that fails to read or render is skipped, never fatal; the result's ``skipped`` lists them. The output is
    sorted and carries no clock or token, so an unchanged vault builds the same bytes every time, which the publisher's
    change detection depends on.
    """
    built = MirrorBuild()
    includes, excludes = _entries(include), _entries(exclude)
    candidates: list[_Candidate] = []
    for root_name, kind in ((NOTES, "notes"), (ARTIFACTS, "html")):
        root = roots.get(root_name)
        if root is None or not any(entry[0] == _key(root_name) for entry in includes):
            continue
        index = vault.VaultIndex.build(Path(root), kind)
        built.truncated = built.truncated or index.truncated
        for item in index.notes:
            if item.missing:
                continue  # a dangling link: nothing to read
            artifact = kind == "html"
            mirror = f"{root_name}/{item.entry_rel if artifact else item.rel}"
            candidates.append(_Candidate(mirror, item, index, artifact))

    pages: list[_Page] = []
    where = _Pages(lexical={}, real={})
    seen: set[str] = set()
    for cand in sorted(candidates, key=lambda c: c.mirror):
        if cand.mirror in seen or not cand.listed(includes, excludes):
            continue
        seen.add(cand.mirror)
        real = Path(os.path.realpath(cand.item.path))
        if real.suffix.lower() in (".md", ".markdown"):
            try:
                if _opted_out(viewer.read_local(real)):
                    continue
            except viewer.ViewerError:
                # Unreadable, so the flag can't be checked: left out, never guessed at.
                built.skipped.append(cand.mirror)
                continue
        page = _Page(cand, real, ids(f"page:{cand.mirror}"))
        pages.append(page)
        where.lexical[str(cand.item.path)] = page.id
        where.real.setdefault(str(real), page.id)

    assets = _Assets(ids, Path(os.path.realpath(home if home is not None else Path.home())))
    used: dict[str, BuiltObject] = {}
    for page in pages:
        try:
            obj, referenced = _build_page(page, where, assets, markdown_css)
        except Exception:  # noqa: BLE001 - one unreadable or unrenderable file must not stop the rest
            # Its assets are not collected: a page that isn't published uploads nothing.
            built.skipped.append(page.cand.mirror)
            continue
        built.append(obj)
        used.update(referenced)
    built.extend(sorted(used.values(), key=lambda asset: asset.name))
    return built


def markdown_theme_css(storage: Any, notes_root: Path | None) -> str | None:
    """The CSS ``/view`` injects into markdown, text and PDF pages for this vault, worked out without a running app.

    None when there is none to inject: no notes vault, no reading theme stored for it, or "Match Obsidian" is off."""
    if notes_root is None:
        return None
    if not storage.settings().get("markdown_follow_obsidian", True):
        return None
    snapshot = storage.markdown_theme(vault.normalize(Path(notes_root).expanduser()))
    if not snapshot:
        return None
    try:
        css = markdown_theme.stylesheet(snapshot)
    except ValueError:
        # A stored theme that no longer validates. The pages publish unthemed, and pick the theme up once it is fixed,
        # since a theme change is a change to their bytes.
        return None
    return css or None

"""Vault mode: a read-only index over an Obsidian-style folder of notes.

The index answers three questions for the reader:
  * what is in the vault (the folder tree and the filter box),
  * where does ``[[Note]]`` / ``![[image.png]]`` point (Obsidian's shortest-path
    resolution, approximated), and
  * is this path inside the vault at all (containment).

Paths are **lexical** end to end. Real vaults contain symlinked folders that
resolve outside the vault root, so every comparison uses ``os.path.normpath`` +
``Path.is_relative_to`` on the path the user sees — never ``Path.resolve()`` and
never ``str.startswith`` (which would accept ``vault-evil`` next to ``vault``).

The same index also serves **Artifacts** (``kind="html"``): a folder of
symlinks to HTML scattered across the disk, browsed the way Obsidian browses
Markdown. Three things differ there, all in service of scanning the list:

  * a page is labelled by its ``<title>``, not its filename — guides are all
    called ``index.html``;
  * below the top level, a folder holding an ``index.html`` IS a page, so a
    guide folder reads as one entry and its ``index.inline.html`` twin, audio
    and notes stay out of the list (they are reachable from the page itself);
  * a symlink whose target is gone stays listed as *missing* rather than
    silently vanishing — in a vault made of links, a moved file is the common
    failure and the list is where you would notice it.

Folders the vault owns stay in the index, and in the sidebar, even while
empty, so there is somewhere to link or drag a page into; a linked folder shows
only once a page sits somewhere beneath it. What the vault owns — an entry
directly in one of its own folders — can be moved, renamed, pinned to the top
of its folder or removed; see *Reorganising Artifacts* below.

A notes vault reorganises by the same rules (``plan_move``, ``move_entry``,
``plan_rename``, ``rename_entry``, ``create_folder``, each with the vault's
``label`` for its messages): only what it owns, never inside a linked folder.
What moving a note does to the links that point at it is ``relink.py``'s.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import viewer

EXCLUDED_DIRS = {"node_modules", "__pycache__"}  # plus any name starting with "."
NOTE_EXTENSIONS = viewer.LOCAL_DOCUMENT_EXTENSIONS
HTML_EXTENSIONS = {".html", ".htm"}
INDEX_NAMES = ("index.html", "index.htm")
IMAGE_EXTENSIONS = {ext for ext, ct in viewer.ASSET_CONTENT_TYPES.items() if ct.startswith("image/")}
ATTACHMENT_EXTENSIONS = set(viewer.ASSET_CONTENT_TYPES)
MAX_ENTRIES = 20_000
MAX_DEPTH = 24
DEFAULT_ATTACHMENT_FOLDER = "Other/Attachments"
VAULT_KINDS = ("notes", "html")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_TITLE_SCAN_BYTES = 64 * 1024
# (path, mtime_ns, size) → title. The index rebuilds every few seconds while the
# vault is open; re-reading every page's head each time would be the cost of
# the whole feature, and a stat is enough to know nothing changed.
_TITLE_CACHE: dict[tuple[str, int, int], str] = {}
_TITLE_CACHE_MAX = 10_000

# SF_DATALESS from <sys/stat.h>: a cloud placeholder (Google Drive, iCloud)
# whose listing or bytes are still with its file provider. Its stat answers at
# once; opening it waits on the provider, over the network, and after the app
# is rebuilt it can wait on a macOS consent prompt as well. Behind
# ~/Documents/CX/Areas/AIN that left the first walk after a restart in open()
# for minutes, so the walks step round placeholders (see _fetch_listings).
SF_DATALESS = 0x40000000
_FETCHING: set[str] = set()
_FETCHING_LOCK = threading.Lock()


def _placeholder(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _fetch_listings(paths: list[str]) -> None:
    """Have the file provider list placeholder folders, on a thread no request waits on.

    A later build then walks them like any other folder, so a linked cloud
    folder fills in once the provider (or the person, at a consent prompt)
    answers, instead of the sidebar waiting for it.
    """
    with _FETCHING_LOCK:
        todo = [path for path in paths if path not in _FETCHING]
        _FETCHING.update(todo)
    if not todo:
        return

    def run() -> None:
        try:
            for path in todo:
                for _walked in os.walk(path, onerror=lambda _e: None):
                    pass
        finally:
            with _FETCHING_LOCK:
                _FETCHING.difference_update(todo)

    threading.Thread(target=run, name="onyx-vault-fetch", daemon=True).start()


def normalize(path: Path | str) -> Path:
    """Collapse ``.``/``..`` segments without touching symlinks."""
    return Path(os.path.normpath(str(path)))


def is_inside(path: Path | str, root: Path | str) -> bool:
    """Lexical containment: ``path`` is ``root`` or lives under it."""
    candidate = normalize(path)
    base = normalize(root)
    if not candidate.is_absolute() or not base.is_absolute():
        return False
    try:
        return candidate == base or candidate.is_relative_to(base)
    except ValueError:
        return False


def html_page_meta(path: Path) -> tuple[str, float] | None:
    """``(title, mtime)`` for an HTML page, following symlinks; None if it is gone.

    The title is the document's ``<title>`` with tags stripped and whitespace
    collapsed, or "" when it has none — the caller decides the fallback, since
    only it knows whether the file stands for itself or for its folder.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if _placeholder(st):
        return "", st.st_mtime  # listed by its name until it is downloaded; reading it would download it
    key = (str(path), st.st_mtime_ns, st.st_size)
    title = _TITLE_CACHE.get(key)
    if title is None:
        try:
            with open(path, "rb") as handle:
                head = handle.read(_TITLE_SCAN_BYTES).decode("utf-8", errors="replace")
        except OSError:
            head = ""
        match = _TITLE_RE.search(head)
        raw = re.sub(r"<[^>]+>", "", match.group(1)) if match else ""
        title = " ".join(html_lib.unescape(raw).split())[:300]
        if len(_TITLE_CACHE) >= _TITLE_CACHE_MAX:
            _TITLE_CACHE.clear()
        _TITLE_CACHE[key] = title
    return title, st.st_mtime


def first_link(path: Path | str, root: Path | str) -> Path | None:
    """The first symlink on ``path`` below ``root``: where the vault hands off.

    Links at or above the root don't count — they are how the vault itself is
    reached, not something it links to.
    """
    lexical = normalize(path)
    base = normalize(root)
    if lexical == base or not is_inside(lexical, base):
        return None
    current = base
    for part in lexical.relative_to(base).parts:
        current = current / part
        if os.path.islink(current):
            return current
    return None


def html_context_folder(path: Path | str, root: Path | str) -> Path | None:
    """The real folder a page in Artifacts draws its evidence from.

    The vault itself is only links, so it is useless as a context folder — the
    provider's search tools would find nothing but symlinks. The first symlink
    on the page's path is where the vault hands off to the real world: a linked
    folder means that folder's target, a linked file means the target's own
    folder. A page with no symlink on its path lives in the vault for real, and
    its top-level project folder is the context.
    """
    lexical = normalize(path)
    base = normalize(root)
    if lexical == base or not is_inside(lexical, base):
        return None
    link = first_link(lexical, base)
    if link is not None:
        try:
            target = link.resolve()
        except (OSError, RuntimeError):
            return None
        return target if target.is_dir() else target.parent
    project = base / lexical.relative_to(base).parts[0]
    return project.resolve() if project.is_dir() else lexical.parent.resolve()


def entry_paths(path: Path | str, root: Path | str) -> dict[str, Any] | None:
    """What a sidebar row's menu reveals and copies; None outside ``root``.

    ``path`` is the row as the tree shows it. ``real`` is the file it actually
    is: where Finder lands when it reveals the row (Finder follows links on the
    way) and what a terminal wants pasted — None when a link on the way is
    dangling. ``link`` is the first symlink below the root, the one thing
    Finder can show from the vault's side; None for a row that lives in the
    vault for real, whose real path is simply its own.
    """
    lexical = normalize(path)
    if lexical == normalize(root) or not is_inside(lexical, root):
        return None
    link = first_link(lexical, root)
    exists = os.path.exists(lexical)
    real = (os.path.realpath(lexical) if link else str(lexical)) if exists else None
    return {
        "path": str(lexical),
        "is_dir": os.path.isdir(lexical),
        "exists": exists,
        "real": real,
        "link": str(link) if link else None,
    }


def reveal_in_finder(target: Path | str) -> None:
    """Select ``target`` in a Finder window (``open -R``)."""
    subprocess.Popen(
        ["/usr/bin/open", "-R", str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _clean_entry_name(name: str) -> str:
    name = str(name or "").strip()
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\x00" in name
        or len(name.encode("utf-8")) > 255
    ):
        raise ValueError("Use a plain name: no slashes, and not starting with a dot.")
    return name


def writable_folder(root: Path | str, rel: str, label: str = "Artifacts") -> Path:
    """Resolve ``rel`` to a folder the app may write into, or raise ValueError.

    The app only ever writes inside the vault's OWN folders. A folder reached
    through a symlink is somebody else's directory — ``Anthropic/guides`` is
    really ``~/learnings/.../guides`` — and creating a link there would write
    into the real tree the vault is meant to point at, not contain.
    """
    base = normalize(root)
    rel = str(rel or "").strip().strip("/")
    parts = [part for part in rel.split("/") if part] if rel else []
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise ValueError(f"That folder is not inside {label}.")
    current = base
    for part in parts:
        current = current / part
        if os.path.islink(current):
            raise ValueError(
                f"“{part}” is a linked folder, so it belongs to another tree. "
                f"Use a folder that lives in {label} instead."
            )
    if not current.is_dir():
        raise ValueError(f"That folder does not exist in {label}.")
    real_base = Path(os.path.realpath(base))
    real_current = Path(os.path.realpath(current))
    if not (real_current == real_base or real_current.is_relative_to(real_base)):
        raise ValueError(f"That folder is not inside {label}.")
    return current


def create_folder(root: Path | str, parent_rel: str, name: str, label: str = "Artifacts") -> Path:
    """Make a new (real) folder in one of the vault's own folders."""
    folder = writable_folder(root, parent_rel, label) / _clean_entry_name(name)
    if os.path.lexists(folder):
        raise ValueError(f"“{folder.name}” already exists there.")
    folder.mkdir()
    return folder


def link_source(root: Path | str, target: Path | str) -> Path:
    """``target`` as something Artifacts can link to — an HTML file or a folder outside it — else ValueError."""
    raw = str(target or "").strip()
    if raw.lower().startswith("file://"):
        from urllib.parse import unquote, urlparse

        raw = unquote(urlparse(raw).path)
    source = normalize(Path(raw).expanduser())
    if not source.is_absolute():
        raise ValueError("Give the full path of the HTML file or folder.")
    if source.is_file():
        if source.suffix.lower() not in HTML_EXTENSIONS:
            raise ValueError("Only HTML files (.html, .htm) or folders can be linked.")
    elif not source.is_dir():
        raise ValueError("That file or folder does not exist.")
    real_vault = Path(os.path.realpath(normalize(root)))
    real_source = Path(os.path.realpath(source))
    if real_source == real_vault or real_source.is_relative_to(real_vault):
        raise ValueError("That is already inside Artifacts.")
    if real_vault.is_relative_to(real_source):
        raise ValueError("That folder contains the Artifacts folder itself, so linking it would loop.")
    return source


def point_link(link: Path, target: Path | str) -> None:
    """Re-aim the symlink ``link`` at ``target`` in one step: a new link made beside it replaces it, so the entry is
    never absent and its name, pins and display name stay as they were. Never touches either target."""
    staged = link.with_name(f".{link.name}.onyx-link")
    if os.path.lexists(staged):
        staged.unlink()
    os.symlink(str(target), str(staged), target_is_directory=os.path.isdir(target))
    os.replace(staged, link)


def retarget_link(root: Path | str, path: Path | str, target: Path | str) -> Path:
    """Point an Artifacts link the vault owns at ``target`` instead — the fix for a page whose file moved. The link keeps
    its name and place; ``target`` must be something ``create_link`` would link. Returns the target linked."""
    entry = owned_entry(root, path)
    if not os.path.islink(entry):
        raise ValueError(f"“{entry.name}” is a folder of Artifacts' own, not a link.")
    source = link_source(root, target)
    point_link(entry, source)
    return source


def create_link(root: Path | str, parent_rel: str, target: Path | str, name: str | None = None) -> Path:
    """Link an HTML file or a folder into Artifacts. Never touches ``target``.

    A linked ``index.html`` is named after its folder, since every guide would
    otherwise arrive as ``index.html`` and collide with the last one.
    """
    folder = writable_folder(root, parent_rel)
    source = link_source(root, target)
    if source.is_dir():
        default = source.name
    else:
        default = (
            f"{source.parent.name}{source.suffix.lower()}"
            if source.name.lower() in INDEX_NAMES and source.parent.name
            else source.name
        )
    link = folder / _clean_entry_name(name or default)
    if source.is_file() and link.suffix.lower() not in HTML_EXTENSIONS:
        link = link.with_name(link.name + source.suffix.lower())
    if os.path.lexists(link):
        raise ValueError(f"“{link.name}” already exists there.")
    os.symlink(str(source), str(link), target_is_directory=source.is_dir())
    return link


# MARK: - Reorganising Artifacts (and the notes vaults)
#
# Moving, renaming, pinning and removing act on what the vault OWNS: an entry
# sitting directly in one of its own folders — a link, or a folder made here.
# Anything deeper, inside a linked folder, is a file in somebody else's tree,
# and handling it would reach through the link into the real folder.

PINS_FILE = ".onyx.json"  # a folder's own settings: {"pinned": [entry names, first first]}


def owned_entry(root: Path | str, path: Path | str, label: str = "Artifacts") -> Path:
    """``path`` if the vault owns it, else ValueError. Lexical, like every path here."""
    base = normalize(root)
    entry = normalize(path)
    if entry == base or not is_inside(entry, base) or entry.name.startswith("."):
        raise ValueError(f"That is not in {label}.")
    if first_link(entry.parent, base) is not None:
        raise ValueError(
            f"“{entry.name}” is inside a linked folder, so it belongs to another tree. Move or rename the link instead."
        )
    parent_rel = entry.parent.relative_to(base).as_posix()  # "." for the top level
    writable_folder(base, "" if parent_rel == "." else parent_rel, label)
    if not os.path.lexists(entry):
        raise ValueError(f"“{entry.name}” is no longer in {label}.")
    return entry


def read_pins(folder: Path) -> list[str]:
    """The entry names pinned to the top of ``folder``, in order."""
    try:
        data = json.loads((folder / PINS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pins = data.get("pinned") if isinstance(data, dict) else None
    return [name for name in pins if isinstance(name, str) and name] if isinstance(pins, list) else []


def _update_pins(folder: Path, change) -> None:
    """Rewrite ``folder``'s pins through ``change(list) -> list``; names no longer there drop out."""
    path = folder / PINS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    before = read_pins(folder)
    after = [name for name in dict.fromkeys(change(list(before))) if os.path.lexists(folder / name)]
    if after == before:
        return
    if after:
        data["pinned"] = after
    else:
        data.pop("pinned", None)
    if not data:
        path.unlink(missing_ok=True)
        return
    staged = folder / f"{PINS_FILE}.tmp"
    staged.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(staged, path)


def _same_entry(a: Path, b: Path) -> bool:
    """Two spellings of one directory entry: a case-only rename on a case-insensitive disk."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _keep_links_pointing(entry: Path) -> None:
    """Make absolute any relative link a move of ``entry`` would re-aim.

    A relative link resolves from wherever it sits, so moving it — or a folder
    holding it — points it somewhere else, unless its target moves along too.
    Links Onyx makes are absolute; this covers ones made by hand (``ln -s ../x``).
    """

    def fix(link: Path) -> None:
        raw = os.readlink(link)
        if os.path.isabs(raw):
            return
        target = normalize(link.parent / raw)
        if link != entry and is_inside(target, entry):
            return  # travels with it
        staged = link.with_name(f".{link.name}.onyx-link")
        os.symlink(str(target), str(staged), target_is_directory=os.path.isdir(link))
        os.replace(staged, link)

    if os.path.islink(entry):
        fix(entry)
    elif entry.is_dir():
        for dirpath, dirnames, filenames in os.walk(entry):  # never follows a link
            for name in dirnames + filenames:
                if os.path.islink(os.path.join(dirpath, name)):
                    fix(Path(dirpath) / name)


def plan_move(root: Path | str, path: Path | str, dest_rel: str, label: str = "Artifacts") -> tuple[Path, Path]:
    """Where moving an entry the vault owns into another of its folders would put it: ``(entry, moved)``, or ValueError.

    Checked before anything changes, so the links a move would break can be worked out first (relink.py).
    """
    entry = owned_entry(root, path, label)
    dest = writable_folder(root, dest_rel, label)
    if dest == entry.parent:
        return entry, entry
    if not os.path.islink(entry) and entry.is_dir() and is_inside(dest, entry):
        raise ValueError(f"“{entry.name}” can't go inside itself.")
    moved = dest / entry.name
    if os.path.lexists(moved):
        raise ValueError(f"“{entry.name}” already exists there.")
    return entry, moved


def move_entry(root: Path | str, path: Path | str, dest_rel: str, label: str = "Artifacts") -> Path:
    """Move an entry the vault owns into another of its folders; its new path.

    ``os.rename`` moves a link itself, never what it points at, so the target
    stays put and the link still resolves from its new folder. A pin and a
    display name move with the entry.
    """
    entry, moved = plan_move(root, path, dest_rel, label)
    if moved == entry:
        return entry
    pinned = entry.name in read_pins(entry.parent)
    shown = read_names(entry.parent).get(entry.name)
    _keep_links_pointing(entry)
    os.rename(entry, moved)
    _update_pins(entry.parent, lambda pins: pins)  # the old name is gone, so it drops out
    _update_names(entry.parent, lambda names: names)
    if pinned:
        _update_pins(moved.parent, lambda pins: pins + [moved.name])
    if shown:
        _update_names(moved.parent, lambda names: {**names, moved.name: shown})
    return moved


_SUFFIX_FAMILIES = (frozenset(HTML_EXTENSIONS), frozenset({".md", ".markdown"}))


def plan_rename(root: Path | str, path: Path | str, name: str, label: str = "Artifacts") -> tuple[Path, Path]:
    """Where renaming an entry the vault owns would put it: ``(entry, renamed)``, or ValueError.

    A file keeps its kind: a page stays ``.html`` and a note ``.md`` unless the new name gives one of the same family,
    so "Plan" renames ``Old.md`` to ``Plan.md``, never to a file Obsidian no longer lists.
    """
    entry = owned_entry(root, path, label)
    new_name = _clean_entry_name(name)
    if not os.path.isdir(entry) and entry.suffix:
        old, new = entry.suffix.lower(), Path(new_name).suffix.lower()
        family = next((f for f in _SUFFIX_FAMILIES if old in f), frozenset({old}))
        if new not in family:
            new_name += entry.suffix
    renamed = entry.parent / new_name
    if renamed != entry and os.path.lexists(renamed) and not _same_entry(entry, renamed):
        raise ValueError(f"“{new_name}” already exists there.")
    return entry, renamed


def rename_entry(root: Path | str, path: Path | str, name: str, label: str = "Artifacts") -> Path:
    """Rename an entry the vault owns, in place; its new path."""
    entry, renamed = plan_rename(root, path, name, label)
    if renamed == entry:
        return entry
    os.rename(entry, renamed)
    new_name = renamed.name
    _update_pins(entry.parent, lambda pins: [new_name if pin == entry.name else pin for pin in pins])
    _update_names(entry.parent, lambda names: {new_name if k == entry.name else k: v for k, v in names.items()})
    return renamed


def read_names(folder: Path) -> dict[str, str]:
    """The names a folder's pages are shown by instead of their titles (Rename on an Artifacts page), by entry name."""
    try:
        data = json.loads((folder / PINS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return {}
    return {k: v for k, v in names.items() if isinstance(k, str) and k and isinstance(v, str) and v.strip()}


def _update_names(folder: Path, change) -> None:
    """Rewrite ``folder``'s display names through ``change(dict) -> dict``; names of entries no longer there drop out."""
    path = folder / PINS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    before = read_names(folder)
    after = {k: v.strip() for k, v in change(dict(before)).items() if v and v.strip() and os.path.lexists(folder / k)}
    if after == before:
        return
    if after:
        data["names"] = after
    else:
        data.pop("names", None)
    if not data:
        path.unlink(missing_ok=True)
        return
    staged = folder / f"{PINS_FILE}.tmp"
    staged.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(staged, path)


def set_display_name(root: Path | str, path: Path | str, name: str) -> Path:
    """Show an Artifacts page by ``name`` rather than its ``<title>``; an empty name goes back to the title.

    The page itself is somebody else's file (the link's target), so its title is never edited: the name lives in its
    folder's ``.onyx.json`` beside the pins, and moves and renames with the link.
    """
    entry = owned_entry(root, path)
    shown = str(name or "").strip()
    if len(shown) > 300 or "\n" in shown:
        raise ValueError("Use a name of one line.")
    _update_names(entry.parent, lambda names: {**names, entry.name: shown})
    return entry


def set_pinned(root: Path | str, path: Path | str, pinned: bool) -> Path:
    """Pin an entry Artifacts owns to the top of its folder, or unpin it."""
    entry = owned_entry(root, path)
    name = entry.name
    if pinned:
        _update_pins(entry.parent, lambda pins: pins if name in pins else pins + [name])
    else:
        _update_pins(entry.parent, lambda pins: [pin for pin in pins if pin != name])
    return entry


def remove_entry(root: Path | str, path: Path | str) -> str:
    """Take a link, or an empty folder, out of Artifacts; which it was: "link" or "folder".

    Only ever the vault's own side: a link goes and its target stays. A real
    file here may be somebody's only copy, so it is refused, never deleted.
    """
    entry = owned_entry(root, path)
    if os.path.islink(entry):
        entry.unlink()
        removed = "link"
    elif entry.is_dir():
        clutter = {PINS_FILE, ".DS_Store"}
        left = [name for name in os.listdir(entry) if name not in clutter]
        if left:
            raise ValueError(f"“{entry.name}” isn't empty. Move or remove what's in it first.")
        for name in clutter:
            (entry / name).unlink(missing_ok=True)
        entry.rmdir()
        removed = "folder"
    else:
        raise ValueError(f"“{entry.name}” is a real file, not a link, so Onyx leaves it alone. Remove it in Finder.")
    _update_pins(entry.parent, lambda pins: pins)
    _update_names(entry.parent, lambda names: names)
    return removed


def read_attachment_folder(root: Path) -> str:
    """Obsidian's ``attachmentFolderPath`` (vault-relative), with its default."""
    try:
        raw = json.loads((root / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        value = str(raw.get("attachmentFolderPath") or "").strip().strip("/")
    except (OSError, ValueError, AttributeError):
        value = ""
    if not value or value.startswith("."):
        return DEFAULT_ATTACHMENT_FOLDER
    return value


# The sidebar's hover preview shows one line under a page's title. It comes
# from the same first 64 KB as the title (see html_page_meta), cached the same way.
SUMMARY_CHARS = 180
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_DEK_RE = re.compile(
    r"""<(p|div|h2|h3)\b[^>]*\bclass\s*=\s*["']?[^"'>]*?\b(?:subtitle|lede|lead|dek|summary|standfirst)\b[^>]*>(.*?)</\1\s*>""",
    re.IGNORECASE | re.DOTALL,
)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.IGNORECASE | re.DOTALL)
_NOISE_RE = re.compile(r"<(script|style|template|svg|title)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL)
_SUMMARY_CACHE: dict[tuple[str, int, int], str] = {}


def _fragment_text(fragment: str) -> str:
    text = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())
    # A tag stripped to a space leaves "built )." — close punctuation back up.
    return re.sub(r"([(\[“‘])\s+", r"\1", re.sub(r"\s+([,.;:!?)\]”’])", r"\1", text))


def html_page_summary(path: Path) -> str:
    """One line to preview a page by, or "": its description, else its subtitle, else its first real paragraph."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if _placeholder(st):
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with open(path, "rb") as handle:
            head = handle.read(_TITLE_SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:
        head = ""
    summary = ""
    for tag in _META_RE.findall(head):
        attrs = {k.lower(): v.strip("\"'") for k, v in _ATTR_RE.findall(tag)}
        if (attrs.get("name") or attrs.get("property") or "").lower() in ("description", "og:description"):
            summary = _fragment_text(attrs.get("content", ""))
            if summary:
                break
    if not summary:
        body = _NOISE_RE.sub(" ", head)
        dek = _DEK_RE.search(body)
        summary = _fragment_text(dek.group(2)) if dek else ""
        if not summary:
            summary = next((t for t in map(_fragment_text, _PARA_RE.findall(body)) if len(t) >= 40), "")
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:—–- ") + "…"
    if len(_SUMMARY_CACHE) >= _TITLE_CACHE_MAX:
        _SUMMARY_CACHE.clear()
    _SUMMARY_CACHE[key] = summary
    return summary


@dataclass(frozen=True)
class VaultFile:
    path: Path  # lexical absolute path — never resolve()d
    rel: str  # posix path relative to the vault root
    name: str
    stem_key: str  # casefolded stem, the wikilink lookup key
    kind: str  # "note" | "attachment"
    # Artifacts only. ``title`` is the display label; ``page_dir`` marks an
    # ``index.html`` standing in for its folder; ``missing`` a dangling link;
    # ``summary`` the one line the sidebar's hover preview shows.
    title: str = ""
    mtime: float = 0.0
    page_dir: bool = False
    missing: bool = False
    summary: str = ""
    named: bool = False  # Artifacts: shown by a name given in Onyx (Rename), not by its title

    @property
    def depth(self) -> int:
        return self.rel.count("/")

    @property
    def folder(self) -> str:
        return self.rel.rsplit("/", 1)[0] if "/" in self.rel else ""

    @property
    def entry_rel(self) -> str:
        """Where the entry sits in the tree: a page folder sits where its folder does."""
        return self.folder if self.page_dir else self.rel

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        if self.page_dir:
            return self.path.parent.name
        return os.path.splitext(self.name)[0] if not self.missing else self.name


def _sort_key(item: VaultFile) -> tuple[int, str]:
    return (item.depth, item.rel.casefold())


@dataclass
class VaultIndex:
    root: Path
    kind: str = "notes"
    files: list[VaultFile] = field(default_factory=list)
    built_at: float = 0.0
    truncated: bool = False
    attachment_folder: str = DEFAULT_ATTACHMENT_FOLDER
    symlinked_dirs: set[str] = field(default_factory=set)
    # Artifacts: folders shown even without a page (see _build_html).
    listed_dirs: list[str] = field(default_factory=list)
    # Artifacts: each of its own folders' pinned entry names (PINS_FILE), by folder rel.
    pins: dict[str, list[str]] = field(default_factory=dict)
    # Artifacts: each of its own folders' display names (Rename on a page), by folder rel, then entry name.
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    # Cloud placeholder folders the walk stepped round (SF_DATALESS), being fetched in the background.
    placeholders: list[str] = field(default_factory=list)
    _by_rel: dict[str, VaultFile] = field(default_factory=dict, repr=False)
    _by_stem: dict[str, list[VaultFile]] = field(default_factory=dict, repr=False)
    _by_name: dict[str, list[VaultFile]] = field(default_factory=dict, repr=False)
    _by_real: dict[str, VaultFile] | None = field(default=None, repr=False)
    _tree: dict[str, Any] = field(default_factory=dict, repr=False)

    # MARK: - Building

    @classmethod
    def build(cls, root: Path, kind: str = "notes") -> "VaultIndex":
        root = normalize(root)
        if kind == "html":
            return cls._build_html(root)
        index = cls(root=root, attachment_folder=read_attachment_folder(root))
        seen: set[tuple[int, int]] = set()
        try:
            st = os.stat(root)
            seen.add((st.st_dev, st.st_ino))
        except OSError:
            index.built_at = time.time()
            index._finish()
            return index
        count = 0
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=True, onerror=lambda _e: None):
            current = Path(dirpath)
            rel_dir = current.relative_to(root).as_posix() if current != root else ""
            depth = rel_dir.count("/") + 1 if rel_dir else 0
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            keep: list[str] = []
            for name in dirnames:
                if name.startswith(".") or name in EXCLUDED_DIRS:
                    continue
                child = os.path.join(dirpath, name)
                try:
                    st = os.stat(child)
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue  # symlink loop or a second link to a folder already walked
                seen.add(key)
                if _placeholder(st):
                    index.placeholders.append(child)
                    continue
                if os.path.islink(child):
                    index.symlinked_dirs.add(f"{rel_dir}/{name}" if rel_dir else name)
                keep.append(name)
            keep.sort(key=str.casefold)
            dirnames[:] = keep
            # A folder with nothing in it still shows, as in Obsidian, so a folder just made takes a drop. One that
            # holds only attachments stays out of the tree, which lists notes.
            if rel_dir and not keep and not any(not f.startswith(".") for f in filenames):
                index.listed_dirs.append(rel_dir)
            for filename in sorted(filenames, key=str.casefold):
                if filename.startswith("."):
                    continue
                ext = os.path.splitext(filename)[1].lower()
                if ext in NOTE_EXTENSIONS:
                    kind = "note"
                elif ext in ATTACHMENT_EXTENSIONS:
                    kind = "attachment"
                else:
                    continue
                count += 1
                if count > MAX_ENTRIES:
                    index.truncated = True
                    break
                rel = f"{rel_dir}/{filename}" if rel_dir else filename
                index.files.append(
                    VaultFile(
                        path=current / filename,
                        rel=rel,
                        name=filename,
                        stem_key=os.path.splitext(filename)[0].casefold(),
                        kind=kind,
                    )
                )
            if index.truncated:
                break
        if index.placeholders:
            _fetch_listings(index.placeholders)
        index.built_at = time.time()
        index._finish()
        return index

    @classmethod
    def _build_html(cls, root: Path) -> "VaultIndex":
        index = cls(root=root, kind="html")
        seen: set[tuple[int, int]] = set()
        try:
            st = os.stat(root)
            seen.add((st.st_dev, st.st_ino))
        except OSError:
            index.built_at = time.time()
            index._finish()
            return index
        count = 0

        def add(path: Path, rel: str, *, page_dir: bool = False) -> bool:
            nonlocal count
            count += 1
            if count > MAX_ENTRIES:
                index.truncated = True
                return False
            meta = html_page_meta(path)
            entry_rel = rel.rpartition("/")[0] if page_dir else rel
            folder_rel, _, entry_name = entry_rel.rpartition("/")
            shown = index.names.get(folder_rel, {}).get(entry_name)
            index.files.append(
                VaultFile(
                    path=path,
                    rel=rel,
                    name=path.name,
                    stem_key=os.path.splitext(path.name)[0].casefold(),
                    kind="note",
                    title=shown or (meta[0] if meta else ""),
                    named=bool(shown),
                    mtime=meta[1] if meta else 0.0,
                    page_dir=page_dir,
                    missing=meta is None,
                    summary=html_page_summary(path) if meta else "",
                )
            )
            return True

        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=True, onerror=lambda _e: None):
            current = Path(dirpath)
            rel_dir = current.relative_to(root).as_posix() if current != root else ""
            depth = rel_dir.count("/") + 1 if rel_dir else 0
            # Below the project level, a folder with an index page is one page.
            index_name = next((n for n in filenames if n.lower() in INDEX_NAMES), None) if depth >= 2 else None
            if index_name is not None:
                dirnames[:] = []
                if rel_dir in index.listed_dirs:
                    index.listed_dirs.remove(rel_dir)  # it is a page now, not a folder
                if not add(current / index_name, f"{rel_dir}/{index_name}", page_dir=True):
                    break
                continue
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            keep: list[str] = []
            for name in dirnames:
                if name.startswith(".") or name in EXCLUDED_DIRS:
                    continue
                child = os.path.join(dirpath, name)
                try:
                    st = os.stat(child)
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                if _placeholder(st):
                    index.placeholders.append(child)
                    continue
                if os.path.islink(child):
                    index.symlinked_dirs.add(f"{rel_dir}/{name}" if rel_dir else name)
                keep.append(name)
            keep.sort(key=str.casefold)
            dirnames[:] = keep
            # Folders the vault owns stay in the index even while empty, so +
            # can offer a folder you just made and a page can be dragged into
            # it. Inside a linked tree only folders that hold pages appear, or
            # a linked repo would list every directory.
            for name in keep:
                child_rel = f"{rel_dir}/{name}" if rel_dir else name
                if depth == 0 or not any(
                    child_rel == linked or child_rel.startswith(linked + "/") for linked in index.symlinked_dirs
                ):
                    index.listed_dirs.append(child_rel)
            if PINS_FILE in filenames and not any(
                rel_dir == linked or rel_dir.startswith(linked + "/") for linked in index.symlinked_dirs
            ):
                index.pins[rel_dir] = read_pins(current)
                index.names[rel_dir] = read_names(current)
            for filename in sorted(filenames, key=str.casefold):
                if filename.startswith("."):
                    continue
                path = current / filename
                rel = f"{rel_dir}/{filename}" if rel_dir else filename
                # os.walk files a dangling link under filenames whatever it once
                # pointed at; keep it visible so the break is noticed.
                dangling = os.path.islink(path) and not os.path.exists(path)
                if not dangling and os.path.splitext(filename)[1].lower() not in HTML_EXTENSIONS:
                    continue
                if not add(path, rel):
                    break
            if index.truncated:
                break
        if index.placeholders:
            _fetch_listings(index.placeholders)
        index.built_at = time.time()
        index._finish()
        return index

    def _finish(self) -> None:
        for item in self.files:
            self._by_rel.setdefault(item.rel.casefold(), item)
            self._by_stem.setdefault(item.stem_key, []).append(item)
            self._by_name.setdefault(item.name.casefold(), []).append(item)
        self._tree = self._build_tree()

    def _build_tree(self) -> dict[str, Any]:
        root_node: dict[str, Any] = {
            "name": self.root.name or str(self.root),
            "path": str(self.root),
            "kind": "dir",
            "children": [],
        }
        dirs: dict[str, dict[str, Any]] = {"": root_node}

        html = self.kind == "html"
        # A pinned node's place among its folder's pins; the sort puts these first.
        rank: dict[int, int] = {}

        def owned(node: dict[str, Any], folder_rel: str, entry: Path) -> None:
            """Mark a row the vault owns (see owned_entry): ``entry`` is what moving it moves."""
            node["entry"] = str(entry)
            pins = self.pins.get(folder_rel) or []
            if entry.name in pins:
                node["pinned"] = True
                rank[id(node)] = pins.index(entry.name)

        def folder_node(rel: str) -> dict[str, Any]:
            node = dirs.get(rel)
            if node is not None:
                return node
            parent_rel, _, name = rel.rpartition("/")
            node = {"name": name, "path": str(self.root / rel), "kind": "dir", "children": []}
            if rel in self.symlinked_dirs:
                node["symlink"] = True
            parent = folder_node(parent_rel)
            # The vault's own folders take a drop, a new folder or a link; a linked one (or anything under it) is
            # another tree — see writable_folder. In Notes as in Artifacts.
            node["rel"] = rel
            node["linked"] = bool(node.get("symlink") or parent.get("linked"))
            if not parent.get("linked"):
                owned(node, parent_rel, self.root / rel)
            dirs[rel] = node
            parent["children"].append(node)
            return node

        for rel in self.listed_dirs:
            folder_node(rel)  # projects and your own folders show before they hold a page

        for item in self.files:
            if item.kind != "note":
                continue
            if not html:
                parent = folder_node(item.folder)
                node = {"name": item.name, "path": str(item.path), "kind": "file", "ext": item.path.suffix.lower()}
                if not parent.get("linked"):
                    owned(node, item.folder, item.path)
                parent["children"].append(node)
                continue
            parent_rel = item.entry_rel.rpartition("/")[0]
            node = {
                "name": item.path.parent.name if item.page_dir else item.name,
                "title": item.label,
                "path": str(item.path),
                "kind": "file",
                "ext": item.path.suffix.lower(),
                "mtime": item.mtime,
            }
            if item.summary:
                node["summary"] = item.summary
            if item.named:
                node["named"] = True
            if item.missing:
                node["missing"] = True
                try:
                    node["target"] = os.readlink(item.path)
                except OSError:
                    pass
            parent = folder_node(parent_rel)
            if not parent.get("linked"):
                # A guide folder's page moves as its folder, the thing sitting in the vault's.
                owned(node, parent_rel, item.path.parent if item.page_dir else item.path)
            parent["children"].append(node)

        def sort(node: dict[str, Any]) -> None:
            node["children"].sort(
                key=lambda n: (
                    (0, rank[id(n)]) if id(n) in rank else (1, 0),
                    0 if n["kind"] == "dir" else 1,
                    (n.get("title") or n["name"]).casefold(),
                )
            )
            for child in node["children"]:
                if child["kind"] == "dir":
                    sort(child)

        sort(root_node)
        root_node["rel"] = ""
        root_node["linked"] = False
        return root_node

    # MARK: - Queries

    @property
    def notes(self) -> list[VaultFile]:
        return [item for item in self.files if item.kind == "note"]

    def contains(self, path: Path | str) -> bool:
        return is_inside(path, self.root)

    def at(self, rel: str) -> VaultFile | None:
        """The row at ``rel``, a path below the root in any case, or None."""
        return self._by_rel.get(rel.casefold())

    def tree_json(self) -> dict[str, Any]:
        return self._tree

    def by_real(self, path: Path | str) -> VaultFile | None:
        """The row a real file shows as: the inverse of ``entry_paths``' ``real``.

        The reading history keys a document by its realpath (``/view`` resolves
        it), so a page in Artifacts comes back as the file its link points at,
        and a note under a symlinked folder as the file outside the vault. This
        finds the row the tree lists it under. Built on first use, since most
        index builds are never asked and each row costs a realpath.
        """
        if self._by_real is None:
            by_real: dict[str, VaultFile] = {}
            for item in self.notes:
                if not item.missing:
                    by_real.setdefault(os.path.realpath(item.path), item)
            self._by_real = by_real
        return self._by_real.get(os.path.realpath(path))

    def _pick(self, candidates: list[VaultFile], source: Path | None) -> VaultFile | None:
        if not candidates:
            return None
        if source is not None:
            source_dir = normalize(source).parent
            same_folder = [item for item in candidates if item.path.parent == source_dir]
            if same_folder:
                return min(same_folder, key=_sort_key)
        if len(candidates) == 1:
            return candidates[0]
        return min(candidates, key=_sort_key)

    def resolve_wikilink(self, target: str, *, source: Path | None = None) -> VaultFile | None:
        """Resolve ``[[target]]`` the way Obsidian's shortest-path setting does.

        A target containing ``/`` is a vault-relative path (with or without
        ``.md``); anything else is a note name matched case-insensitively by
        stem. Ties prefer the linking note's own folder, then the shallowest,
        alphabetically-first match.
        """
        target = target.strip().strip("/")
        if not target:
            return None
        if target.casefold().endswith(".md"):
            target = target[:-3]
        key = target.casefold()
        if "/" in target:
            for candidate in (key, f"{key}.md", *(f"{key}{ext}" for ext in NOTE_EXTENSIONS)):
                item = self._by_rel.get(candidate)
                if item is not None and item.kind == "note":
                    return item
            suffix = "/" + key
            matches = [
                item
                for item in self.notes
                if item.rel.casefold().endswith((suffix, f"{suffix}.md"))
            ]
            return self._pick(matches, source)
        candidates = [item for item in self._by_stem.get(key, []) if item.kind == "note"]
        markdown = [item for item in candidates if item.path.suffix.lower() in {".md", ".markdown"}]
        return self._pick(markdown or candidates, source)

    def resolve_embed(self, target: str, *, source: Path | None = None) -> VaultFile | None:
        """Resolve ``![[target]]``: attachments by full filename, notes by name."""
        target = target.strip().strip("/")
        if not target:
            return None
        key = target.casefold()
        if "/" in target:
            item = self._by_rel.get(key)
            if item is not None:
                return item
            key = key.rsplit("/", 1)[1]
        candidates = list(self._by_name.get(key, []))
        if candidates:
            attachment_prefix = self.attachment_folder.casefold() + "/"
            in_attachments = [
                item for item in candidates if item.rel.casefold().startswith(attachment_prefix)
            ]
            if in_attachments:
                return min(in_attachments, key=_sort_key)
            return self._pick(candidates, source)
        if not os.path.splitext(target)[1]:
            return self.resolve_wikilink(target, source=source)
        return None

    def search(self, query: str, *, limit: int = 50) -> tuple[list[VaultFile], bool]:
        needle = query.strip().casefold()
        if not needle:
            return [], False
        prefix: list[VaultFile] = []
        other: list[VaultFile] = []
        for item in self.notes:
            if item.missing:
                continue  # nothing to open; the tree is where a broken link shows
            label = item.label.casefold() if self.kind == "html" else ""
            if item.stem_key.startswith(needle) or (label and label.startswith(needle)):
                prefix.append(item)
            elif needle in item.name.casefold() or needle in item.rel.casefold() or needle in label:
                other.append(item)
        prefix.sort(key=_sort_key)
        other.sort(key=lambda item: (len(item.rel), item.rel.casefold()))
        results = prefix + other
        return results[:limit], len(results) > limit


class VaultCache:
    """Rebuilds each index at most once per ``ttl`` seconds; thread-safe.

    Each vault builds under a lock of its own. A Notes walk can wait on the disk
    for minutes, and with one lock shared by both kinds the Artifacts tree
    queued behind it: the sidebar sat on "Loading…" after a restart. The same
    holds between notes vaults, so the lock and the slot are per (kind, root).
    """

    def __init__(self, ttl: float = 5.0) -> None:
        self.ttl = ttl
        # Guards the fields below and is never held across a build, so
        # invalidate(), which the mutation routes call on the event loop, never
        # waits on a walk.
        self._state = threading.Lock()
        self._building: dict[tuple[str, Path], threading.Lock] = {}
        # One slot per vault, so switching between Notes and HTML, or reading
        # a note in another notes vault, does not throw the other index away.
        self._indexes: dict[tuple[str, Path], VaultIndex] = {}
        self._generation = 0
        # Run before each walk, with (root, kind): the app repairs Artifacts links whose pages moved (link_repair.py),
        # so the listing that follows shows them working rather than missing.
        self.prepare: Callable[[Path, str], None] | None = None

    def get(self, root: Path, kind: str = "notes") -> VaultIndex:
        root = normalize(root)
        slot = (kind, root)
        with self._state:
            building = self._building.setdefault(slot, threading.Lock())
        with building:
            with self._state:
                cached = self._indexes.get(slot)
                generation = self._generation
            if cached is not None and time.time() - cached.built_at < self.ttl:
                return cached
            if self.prepare is not None:
                self.prepare(root, kind)
            index = VaultIndex.build(root, kind=kind)
            with self._state:
                # A walk that began before an invalidate() may predate the change; its caller gets it, the cache doesn't.
                if generation == self._generation:
                    self._indexes[slot] = index
            return index

    def invalidate(self) -> None:
        with self._state:
            self._generation += 1
            self._indexes.clear()

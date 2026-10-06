"""Artifacts links follow a page moved or renamed outside Onyx — in Finder, by another app, by ``mv``.

Artifacts is a folder of symlinks, and a symlink holds a path: move the file and the link points at nothing, and the
page shows as missing. macOS still knows the file, though. Every file and folder has an identity on its disk (device
and file number) that a move or rename on that disk keeps, and ``/.vol/<device>/<number>`` reaches it wherever it now
is. So each listing of Artifacts records, for every link that works, what its target is (``sweep``), and when one
stops working, looks that identity up and re-aims the link at where the file went — but only when the answer is
certain: the same kind (a folder for a folder, an HTML page for a page), still outside Artifacts, and not in the Trash,
where a deleted file also keeps its identity.

The identities live in Onyx's own database, never beside the links: a file number means nothing on another Mac the
Artifacts folder is copied or synced to. A link that broke before anything was recorded — or whose file moved to
another disk, which gives it a new identity — gets a guess instead (``candidates``): a page or folder of the same
name, near where it was or anywhere Spotlight knows, offered from the sidebar's row menu and linked only when chosen.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import vault

logger = logging.getLogger("onyx.link_repair")

F_GETPATH = 50  # <sys/fcntl.h>: the path an open file descriptor now has
O_EVTONLY = 0x8000  # open for its identity only: no read access needed, and a cloud file is not downloaded
TRASH_DIRS = {".Trash", ".Trashes"}
NEARBY_LEVELS = 2  # how far above the old folder the guess looks
NEARBY_DEPTH = 4  # and how deep below that
NEARBY_LIMIT = 20_000  # directory entries, so a guess over a large tree stays quick
MAX_CANDIDATES = 8

Identity = tuple[str, int, int, bool]  # target, dev, ino, is_dir — as Storage.link_targets keeps them


def locate(dev: int, ino: int) -> str | None:
    """Where the file with this identity is now, or None if the disk doesn't know it (or this isn't macOS)."""
    if sys.platform != "darwin":
        return None
    try:
        fd = os.open(f"/.vol/{int(dev)}/{int(ino)}", O_EVTONLY)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
    except OSError:
        return None
    finally:
        os.close(fd)
    path = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    return path or None


def in_trash(path: Path | str) -> bool:
    return any(part in TRASH_DIRS for part in Path(path).parts)


def fits(root: Path, path: Path | str, is_dir: bool) -> bool:
    """Whether ``path`` can stand in for a link's lost target: the same kind, outside Artifacts, not in the Trash."""
    path = Path(path)
    if in_trash(path) or os.path.isdir(path) != is_dir or not os.path.exists(path):
        return False
    if not is_dir and path.suffix.lower() not in vault.HTML_EXTENSIONS:
        return False
    try:
        vault.link_source(root, path)
    except ValueError:
        return False
    return True


def owned_links(root: Path) -> list[Path]:
    """Every symlink the vault owns: in its own folders, never inside a linked one (that is another tree's)."""
    links: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda _e: None):  # never follows a link
        dirnames[:] = [n for n in dirnames if not n.startswith(".")]
        for name in dirnames + filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if os.path.islink(path):
                links.append(path)
    return links


def _identity(link: Path) -> Identity | None:
    try:
        st = os.stat(link)
    except OSError:
        return None
    return os.path.realpath(link), st.st_dev, st.st_ino, os.path.isdir(link)


def sweep(root: Path | str, storage) -> list[tuple[str, str]]:
    """Record where every Artifacts link points, and re-aim each broken one whose file can be found for certain.
    Returns the links repaired, as (link, new target). Writes to the database only what changed."""
    root = vault.normalize(root)
    known = storage.link_targets(str(root))
    remember: dict[str, Identity] = {}
    seen: set[str] = set()
    repaired: list[tuple[str, str]] = []
    for link in owned_links(root):
        key = str(link)
        seen.add(key)
        now = _identity(link)
        if now is None:
            was = known.get(key)
            if was is None:
                continue
            found = locate(was[1], was[2])
            if found is None or not fits(root, found, was[3]):
                continue
            try:
                vault.point_link(link, found)
            except OSError as exc:
                logger.warning("could not relink %s to %s: %s", link, found, exc)
                continue
            logger.info("relinked %s: its target moved from %s to %s", link, was[0], found)
            repaired.append((key, found))
            now = _identity(link)
            if now is None:
                continue
        if known.get(key) != now:
            remember[key] = now
    storage.remember_link_targets(remember, [k for k in known if k not in seen])
    return repaired


def spotlight(name: str) -> list[str]:
    """Paths Spotlight knows by exactly this name. Empty when it is off, slow or not on this Mac."""
    try:
        done = subprocess.run(["mdfind", "-name", name], capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in done.stdout.splitlines() if os.path.basename(line) == name]


def _nearby(start: Path, name: str) -> list[str]:
    """Paths named ``name`` around ``start``: a few levels above where the file was, and a few below that."""
    top = start
    for _ in range(NEARBY_LEVELS):
        if top.parent == top or top.parent == Path.home().parent:
            break
        top = top.parent
    while not top.is_dir() and top.parent != top:
        top = top.parent
    if top in (Path("/"), Path.home()):
        return []  # too wide to walk on a right-click; Spotlight covers it
    found: list[str] = []
    seen = 0
    base = len(top.parts)
    for dirpath, dirnames, filenames in os.walk(str(top), onerror=lambda _e: None):
        seen += len(dirnames) + len(filenames)
        if seen > NEARBY_LIMIT:
            break
        here = Path(dirpath)
        dirnames[:] = [n for n in dirnames if not n.startswith(".") and n not in vault.EXCLUDED_DIRS]
        if len(here.parts) - base >= NEARBY_DEPTH:
            dirnames[:] = []
        if name in dirnames or name in filenames:
            found.append(str(here / name))
    return found


def candidates(root: Path | str, path: Path | str, finder: Callable[[str], list[str]] = spotlight) -> dict:
    """Where a broken link's page might have gone: ``{"was", "near", "is_dir", "candidates"}`` (``near`` is the closest
    folder of the old path that still exists, where a picker starts). A guess, never acted on here.

    A page is matched by its file name; a guide's ``index.html`` (every guide has one) by its folder's name too.
    """
    root = vault.normalize(root)
    entry = vault.owned_entry(root, path)
    if not os.path.islink(entry):
        raise ValueError(f"“{entry.name}” is not a link.")
    was = vault.normalize(entry.parent / os.readlink(entry))
    near = was.parent
    while not near.is_dir() and near.parent != near:
        near = near.parent
    if os.path.exists(entry):
        return {"was": str(was), "near": str(near), "is_dir": os.path.isdir(entry), "candidates": []}
    is_dir = was.suffix.lower() not in vault.HTML_EXTENSIONS
    index_page = not is_dir and was.name.lower() in vault.INDEX_NAMES
    pool = _nearby(was.parent, was.name) + finder(was.name)
    out: list[str] = []
    for raw in pool:
        found = vault.normalize(raw)
        if str(found) in out or found == was or not fits(root, found, is_dir):
            continue
        if index_page and found.parent.name != was.parent.name:
            continue
        out.append(str(found))
        if len(out) >= MAX_CANDIDATES:
            break
    return {"was": str(was), "near": str(near), "is_dir": is_dir, "candidates": out}

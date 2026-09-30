"""Publish the mirror: build it, seal what changed, upload that, then the index, then delete what is gone.

The order is the point. A phone reads the index to learn what to fetch, so every object it names must
already be in the store when the new index lands, and an object is deleted only after the index that
stopped naming it. A blob is re-sealed only when its plaintext changed: AES-GCM under a fresh nonce
makes a different blob every time, and a different blob is a download on the phone.

What has been uploaded is kept in ``<data dir>/mirror/state-<destination>.json``: per object the hash
of its plaintext and of its sealed blob, all hex, nothing readable. It is per destination so a dry run
into a folder never tells the real bucket that everything is already there.

A page the builder could not read this time (``skipped``) is not treated as deleted: its last published
object stays in the store and its last index entry stays in the index. After a cut-short vault walk
(``truncated``) nothing at all is deleted, and every entry the walk didn't reach stays in the index. That entry comes from a sealed copy of the last index kept beside the state, which
is ciphertext on disk like everything else the mirror keeps.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import secrets as keychain
from .config import MirrorConfig
from .crypto import Keys, deflate_raw, inflate_raw
from .errors import MirrorError, MirrorNotConfigured
from .store import Store

STATE_VERSION = 1
INDEX_VERSION = 1
MAX_TEXT = 20_000
KINDS = ("markdown", "html", "text")


@dataclass(frozen=True)
class PublishReport:
    """Counts, and nothing else: no path, title, id or credential ever lands in a report or a log."""

    pages: int
    assets: int
    uploaded: int  # pages and assets sealed and sent this run; ``unchanged`` is the rest of them
    unchanged: int
    deleted: int
    bytes: int  # everything sent this run, the index included
    index_uploaded: bool


def mirror_dir(data_dir: Path) -> Path:
    folder = Path(data_dir) / "mirror"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def state_path(data_dir: Path, destination: str) -> Path:
    return mirror_dir(data_dir) / f"state-{destination}.json"


@contextlib.contextmanager
def locked(data_dir: Path) -> Iterator[None]:
    """One publish (or wipe) at a time, whether it is the server's thread or a CLI run that asked."""
    fd = os.open(mirror_dir(data_dir) / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def load_state(path: Path) -> dict:
    empty = {"v": STATE_VERSION, "objects": {}, "index": None, "last": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict) or state.get("v") != STATE_VERSION or not isinstance(state.get("objects"), dict):
        return empty
    return {**empty, **state}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
    os.replace(tmp, path)


def sealed_index_path(data_dir: Path, destination: str) -> Path:
    return mirror_dir(data_dir) / f"index-{destination}.blob"


def clear_state(data_dir: Path, destination: str) -> None:
    state_path(data_dir, destination).unlink(missing_ok=True)
    sealed_index_path(data_dir, destination).unlink(missing_ok=True)


def last_published(data_dir: Path) -> dict | None:
    """The newest real-bucket publish on record (``status``), or None. Dry runs into folders don't count."""
    folder = Path(data_dir) / "mirror"
    if not folder.is_dir():  # a read, so it must not be the thing that creates the directory
        return None
    newest = None
    for path in folder.glob("state-r2-*.json"):
        last = load_state(path).get("last")
        if isinstance(last, dict) and (newest is None or str(last.get("at")) > str(newest.get("at"))):
            newest = last
    return newest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _carried(data_dir: Path, destination: str, keys: Keys, current: dict, known: dict, skipped: set[str],
             truncated: bool) -> tuple[list[dict], list[dict]]:
    """Last index's entries for what this build left out but must not lose: skipped pages (or all of them after a
    cut-short walk), and, since a skipped page's assets were not collected either, every asset that is missing.

    Only an entry whose object is still in the store at the hash the entry names is carried. With no readable
    previous index nothing is carried, which is the ordinary behaviour.
    """
    try:
        blob = sealed_index_path(data_dir, destination).read_bytes()
        previous = json.loads(inflate_raw(keys.open_blob(keys.ident("index"), blob)))
        pages, assets = previous["pages"], previous["assets"]
    except (OSError, ValueError, KeyError, TypeError, MirrorError):
        return [], []

    def live(entry: dict) -> bool:
        return entry["id"] not in current and known.get(entry["id"], {}).get("sha") == entry["sha"]

    try:
        return ([e for e in pages if live(e) and (truncated or e["path"] in skipped)],
                [e for e in assets if live(e)])
    except (KeyError, TypeError):
        return [], []


def _index_entries(current: dict, known: dict) -> tuple[list[dict], list[dict]]:
    pages, assets = [], []
    for id_, obj in current.items():
        sealed = known[id_]
        if obj.page is None:
            assets.append({"id": id_, "mime": obj.mime, "sha": sealed["sha"], "size": sealed["size"]})
            continue
        page = obj.page
        if page["kind"] not in KINDS:
            raise MirrorError("a built page has an unknown kind")
        pages.append({
            "id": id_, "path": page["path"], "title": page["title"], "kind": page["kind"],
            "sha": sealed["sha"], "size": sealed["size"], "mtime": float(page["mtime"]),
            "text": str(page["text"])[:MAX_TEXT],
        })
    pages.sort(key=lambda entry: entry["path"])
    assets.sort(key=lambda entry: entry["id"])
    return pages, assets


def publish(*, config: MirrorConfig, secrets: keychain.Secrets, store: Store, roots: dict[str, Path],
            markdown_css: str | None, data_dir: Path, build: Callable | None = None,
            now: datetime | None = None) -> PublishReport:
    master = secrets.get(keychain.MASTER_KEY)
    if not master:
        raise MirrorNotConfigured("master_key is missing from the Keychain (run `onyx mirror setup --generate`)")
    keys = Keys.from_b64url(master)
    if build is None:
        from .build import build_mirror as build  # written beside this module; imported late so this one stands alone
    published_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with locked(data_dir):
        built = build(roots=roots, include=list(config.include), exclude=list(config.exclude),
                      ids=keys.ident, markdown_css=markdown_css)
        current = {}
        for obj in built:
            current.setdefault(obj.id, obj)

        path = state_path(data_dir, store.destination)
        state = load_state(path)
        known: dict = state["objects"]
        if not current and known:
            # A vault that has gone offline (a synced drive that dropped) walks as empty. Deleting every
            # copy on the phone because of that would be the worst possible reading of it.
            raise MirrorError("nothing matched the include list, but objects are already published; "
                              "run `onyx mirror wipe` if you mean to clear the mirror")

        uploaded = sent = 0
        index_uploaded = False
        dirty = False
        try:
            for id_, obj in current.items():
                plain = _sha(obj.data)
                if known.get(id_, {}).get("psha") == plain:
                    continue
                blob = keys.seal(id_, obj.data)
                store.put(id_, blob)
                known[id_] = {"psha": plain, "sha": _sha(blob), "size": len(blob)}
                uploaded += 1
                sent += len(blob)
                dirty = True

            pages, assets = _index_entries(current, known)
            index_id = keys.ident("index")
            skipped = set(getattr(built, "skipped", ()) or ())
            truncated = bool(getattr(built, "truncated", False))
            keep = {keys.ident(f"page:{mirror_path}") for mirror_path in skipped}
            if skipped or truncated:
                more_pages, more_assets = _carried(data_dir, store.destination, keys, current, known, skipped, truncated)
                pages = sorted(pages + more_pages, key=lambda entry: entry["path"])
                assets = sorted(assets + more_assets, key=lambda entry: entry["id"])
                keep |= {entry["id"] for entry in (*more_pages, *more_assets)}
            body = {"v": INDEX_VERSION, "pages": pages, "assets": assets}
            # Changes, not time: a publish that finds nothing new leaves the index, and the phone's copy, alone.
            key = _sha((index_id + "\0" + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode("utf-8"))
            if (state["index"] or {}).get("key") != key:
                index = {"v": INDEX_VERSION, "published_at": published_at, "pages": pages, "assets": assets}
                blob = keys.seal(index_id, deflate_raw(json.dumps(index, ensure_ascii=False).encode("utf-8")))
                store.put(index_id, blob)
                _write_private(sealed_index_path(data_dir, store.destination), blob)
                state["index"] = {"key": key, "at": published_at}
                index_uploaded = True
                sent += len(blob)
                dirty = True

            deleted = 0
            # After a cut-short walk whatever is missing was never looked at, so nothing is deleted, whether or not
            # the last index was there to carry its entry over.
            stale = [] if truncated else [i for i in known if i not in current and i not in keep]
            for id_ in stale:
                store.delete(id_)
                del known[id_]
                deleted += 1
                dirty = True
            if dirty:
                state["last"] = {"at": published_at, "pages": len(pages), "assets": len(assets), "uploaded": uploaded,
                                 "unchanged": len(current) - uploaded, "deleted": deleted, "bytes": sent}
        finally:
            # Also on a failure half way: what did reach the store is recorded, so the next run neither
            # resends it nor forgets to delete it.
            if dirty:
                save_state(path, state)

        return PublishReport(pages=len(pages), assets=len(assets), uploaded=uploaded,
                             unchanged=len(current) - uploaded, deleted=deleted, bytes=sent,
                             index_uploaded=index_uploaded)

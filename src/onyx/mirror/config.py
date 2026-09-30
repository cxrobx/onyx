"""``mirror.toml``: the one switch that turns the phone mirror on.

The file lives in the data directory next to ``onyx.db`` and holds no secret (those are in the
Keychain, ``secrets.py``). Anything short of a well-formed file that says ``enabled = true`` and names
what to publish loads as ``None``, and ``None`` means the mirror does nothing at all.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .. import storage

FILENAME = "mirror.toml"
DEFAULT_INTERVAL_MINUTES = 5
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440


@dataclass(frozen=True)
class MirrorConfig:
    enabled: bool
    interval_minutes: int
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    # Recent asks go to the phone only when mirror.toml says `chats = true`: an answer can quote whatever its context
    # folder held, which reaches wider than the page it was asked on.
    chats: bool = False


def path_for(data_dir: Path | None = None) -> Path:
    return (data_dir or storage.default_data_dir()) / FILENAME


def _paths(value: object) -> tuple[str, ...] | None:
    """A list of non-empty strings, or None for anything else.

    A blank entry is refused rather than skipped: a prefix match on "" would publish everything, and a
    config this malformed should do nothing rather than guess.
    """
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not isinstance(item, str):
            return None
        item = item.strip().strip("/")
        if not item:
            return None
        out.append(item)
    return tuple(out)


def _interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, value))


def _read(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def load(data_dir: Path | None = None) -> MirrorConfig | None:
    """The mirror's config, or None when it is missing, unreadable, off, or names nothing to publish."""
    raw = _read(path_for(data_dir))
    if raw is None or raw.get("enabled") is not True:
        return None
    include = _paths(raw.get("include"))
    exclude = _paths(raw.get("exclude", []))
    if not include or exclude is None:
        return None
    return MirrorConfig(
        enabled=True,
        interval_minutes=_interval(raw.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)),
        include=include,
        exclude=exclude,
        chats=raw.get("chats") is True,
    )


def _quote(value: str) -> str:
    # JSON's string escapes are a subset of TOML's basic strings, so this is always a valid TOML value.
    return json.dumps(value, ensure_ascii=False)


def render(*, enabled: bool, interval_minutes: int, include: tuple[str, ...] | list[str],
           exclude: tuple[str, ...] | list[str], chats: bool = False) -> str:
    """The file's text. Used for the setup template and to rebuild a file ``disable`` cannot edit in place."""
    return (
        "# The Onyx phone mirror (docs/plans/phone-mirror.md). Nothing is published unless enabled = true.\n"
        "# Credentials are not kept here; `onyx mirror setup` puts them in the Keychain.\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        f"interval_minutes = {interval_minutes}\n"
        '# Mirror paths to publish: "Notes/<folder>" for the notes vault, "Artifacts/<folder>" for Artifacts,\n'
        '# "Notes" or "Artifacts" for a whole tree. There is no default: nothing is published until you list it.\n'
        f"include = [{', '.join(_quote(p) for p in include)}]\n"
        "# Paths under these are never published, even when an include covers them.\n"
        f"exclude = [{', '.join(_quote(p) for p in exclude)}]\n"
        "# Recent asks (and their answers) go to the phone only when this is true; only asks on published pages.\n"
        f"chats = {'true' if chats else 'false'}\n"
    )


def _write(path: Path, text: str) -> None:
    """Replace ``path`` whole, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_template(data_dir: Path | None = None) -> bool:
    """Write a disabled template if there is no file yet. True when it wrote one."""
    path = path_for(data_dir)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, render(enabled=False, interval_minutes=DEFAULT_INTERVAL_MINUTES, include=[], exclude=[]))
    return True


def disable(data_dir: Path | None = None) -> bool:
    """Set ``enabled = false`` in place. True when a file was there to change.

    The kill switch must not fail quietly, so the edit is checked by parsing the result; a file too odd
    for the one-line edit to reach is rewritten from what it parsed to. A file that does not parse is
    already inert (``load`` returns None for it) and is left for its owner to fix.
    """
    path = path_for(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if _read(path) is None:
        return True
    _write(path, re.sub(r"(?m)^(\s*enabled\s*=\s*)true\b", r"\1false", text, count=1))
    raw = _read(path) or {}
    if raw.get("enabled") is not False:
        _write(path, render(enabled=False, interval_minutes=_interval(raw.get("interval_minutes")),
                            include=_paths(raw.get("include")) or (), exclude=_paths(raw.get("exclude", [])) or (),
                            chats=raw.get("chats") is True))
    return True

"""The server's background publisher.

``start`` returns None, and starts nothing, unless ``mirror.toml`` loads as enabled. Once running, the
thread reads the file again each cycle, so ``onyx mirror stop`` silences it within one interval
without a restart. A failure is logged as a type or one of this package's own messages (``errors.
describe_error``), never as a library's text, because that text can quote an endpoint or a path.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from . import config as mirror_config
from . import secrets as keychain
from .errors import describe_error
from .publish import publish
from .store import R2Store

log = logging.getLogger("onyx.mirror")

# The first publish waits so a launch isn't slowed by walking the vault while the window opens.
INITIAL_DELAY = 30.0
THREAD_NAME = "onyx-mirror-publisher"


def roots_from_settings(settings: dict) -> dict[str, Path]:
    """The two trees as the app's settings name them (the same meaning as ``app._vault_root``), missing ones left out."""
    from .. import vault

    roots: dict[str, Path] = {}
    for label, key in (("Notes", "vault_root"), ("Artifacts", "html_vault_root")):
        raw = str(settings.get(key) or "").strip()
        if not raw:
            continue
        root = vault.normalize(Path(raw).expanduser())
        try:
            if root.is_absolute() and root.is_dir():
                roots[label] = root
        except OSError:
            continue
    return roots


def markdown_css_for(storage, roots: dict[str, Path]) -> str | None:
    """The vault's Markdown theme as CSS, so a mirrored note looks like the app's. None if the builder lacks it."""
    try:
        from .build import markdown_theme_css
    except ImportError:
        return None
    return markdown_theme_css(storage, roots.get("Notes"))


def library_for(storage, config, look: dict | None) -> Callable:
    """The publisher's hook for the Library object: recent pages always, recent asks when `chats = true`.

    ``look`` is the index's look; its vault palette dresses the thread pages as the Mac's own chrome is dressed.
    """
    from .library import build_library

    vault = (look or {}).get("vault")

    def build(built, ids):
        return build_library(storage, built=built, ids=ids, look=vault, chats=config.chats)

    return build


def look_for(storage, roots: dict[str, Path]) -> dict:
    """What the Mac app wears, for the phone's chrome: the index's ``look`` (wire format, "Look").

    ``vault`` is the palette ``app.current_vault_look`` wears, under the same switch ("Match vault appearance" is
    either follow setting) and in the mode the Mac's Color theme picks (vault_mode), so the phone shows the vault
    exactly when and as the Mac does. ``vaults`` is the vault's palette in each mode the plugin has measured, for the
    phone's own Appearance setting.
    """
    from .. import vault_look, vault_mode

    settings = storage.settings()
    notes = roots.get("Notes")
    enabled = bool(settings.get("markdown_follow_obsidian", True) or settings.get("sidebar_follow_obsidian", True))
    vault = vaults = None
    if enabled and notes is not None:
        vault = vault_look.palette(*vault_mode.snapshots(storage, notes, settings.get("vault_mode")))
        vaults = {}
        for mode in ("light", "dark"):
            markdown = storage.markdown_theme(notes, mode)
            vaults[mode] = vault_look.palette(markdown, storage.sidebar_theme(notes, mode)) if markdown else None
    appearance = settings.get("appearance_theme")
    return {
        "vault": vault,
        "vaults": vaults,
        "follow_page": bool(settings.get("html_follow_page", True)),
        "appearance": appearance if appearance in ("system", "light", "dark") else "system",
    }


class Publisher:
    def __init__(self, storage, roots: Callable[[], dict[str, Path]], *,
                 secrets: keychain.Secrets | None = None) -> None:
        self._storage = storage
        self._roots = roots
        self._secrets = secrets or keychain.KeychainSecrets()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_failure: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        if self._stop.wait(INITIAL_DELAY):
            return
        while True:
            minutes = self.cycle()
            if self._stop.wait(minutes * 60):
                return

    def cycle(self) -> int:
        """One publish if the config still says so. Returns the minutes to wait before the next."""
        config = mirror_config.load(self._storage.data_dir)
        if config is None:
            return mirror_config.DEFAULT_INTERVAL_MINUTES
        try:
            roots = self._roots()
            look = look_for(self._storage, roots)
            report = publish(
                config=config, secrets=self._secrets, store=R2Store.from_secrets(self._secrets),
                roots=roots, markdown_css=markdown_css_for(self._storage, roots), data_dir=self._storage.data_dir,
                look=look,
                library=library_for(self._storage, config, look),
            )
        except Exception as exc:  # the thread must outlive one bad cycle
            message = describe_error(exc)
            if message != self._last_failure:  # one line per distinct failure, not one every interval
                log.warning("mirror: publish failed (%s)", message)
            self._last_failure = message
        else:
            self._last_failure = None
            if report.uploaded or report.deleted or report.index_uploaded:
                log.info("mirror: published %d pages and %d assets (%d uploaded, %d deleted)",
                         report.pages, report.assets, report.uploaded, report.deleted)
        return config.interval_minutes


def start(storage, roots: Callable[[], dict[str, Path]], *, secrets: keychain.Secrets | None = None) -> Publisher | None:
    """Start the publisher if the mirror is on; otherwise do nothing at all (no thread, no file, no network)."""
    if mirror_config.load(storage.data_dir) is None:
        return None
    publisher = Publisher(storage, roots, secrets=secrets)
    publisher.start()
    return publisher

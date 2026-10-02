"""Which of the vault's colour modes Onyx wears while Match vault appearance is on (Settings ▸ Color theme).

The plugin sends the mode Obsidian is showing and, measured beside it, the other one (integrations/obsidian
color-mode.ts); storage keeps both per vault. The setting ``vault_mode`` picks:

- ``obsidian``: whichever mode Obsidian is in, as Onyx always did;
- ``light`` / ``dark``: that mode of the vault's theme;
- ``system``: the mode macOS is in. Read through CoreFoundation and re-read at most once a second, so the shell's
  3-second look poll (vault_ui ``syncSidebarTheme``) follows a switch at sunset with nothing pushed to it.

A mode the plugin hasn't measured yet (an older plugin, or no sync since it was updated) falls back to the one
there is, so the app never loses the vault look over it. The reading styles, the sidebar and the palette always
come from one mode together: mixing them would pair one mode's ink with the other's ground.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from pathlib import Path
from typing import Any

CHOICES = ("obsidian", "system", "light", "dark")
_CACHE_SECONDS = 1.0
_cache: tuple[float, str] | None = None
_lock = threading.Lock()


def _read_interface_style() -> str | None:
    """``AppleInterfaceStyle`` from the global preferences: "Dark" while macOS is dark, None when light or unreadable."""
    path = ctypes.util.find_library("CoreFoundation")
    if not path:
        return None
    try:
        cf = ctypes.cdll.LoadLibrary(path)
        void, utf8 = ctypes.c_void_p, 0x08000100  # kCFStringEncodingUTF8
        cf.CFStringCreateWithCString.restype = void
        cf.CFStringCreateWithCString.argtypes = [void, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFPreferencesAppSynchronize.argtypes = [void]
        cf.CFPreferencesCopyAppValue.restype = void
        cf.CFPreferencesCopyAppValue.argtypes = [void, void]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [void]
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetCString.argtypes = [void, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [void]
        anyapp = void.in_dll(cf, "kCFPreferencesAnyApplication")
        key = cf.CFStringCreateWithCString(None, b"AppleInterfaceStyle", utf8)
        if not key:
            return None
        try:
            # Another process (System Settings, or the switch at sunset) wrote it: drop this process's cached copy.
            cf.CFPreferencesAppSynchronize(anyapp)
            value = cf.CFPreferencesCopyAppValue(key, anyapp)
        finally:
            cf.CFRelease(key)
        if not value:
            return None
        try:
            if cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                return None
            buffer = ctypes.create_string_buffer(64)
            return buffer.value.decode() if cf.CFStringGetCString(value, buffer, len(buffer), utf8) else None
        finally:
            cf.CFRelease(value)
    except (OSError, AttributeError, ValueError):
        return None


def system_mode() -> str:
    """macOS's light or dark right now; "light" anywhere it can't be read."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache and now - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        mode = "dark" if _read_interface_style() == "Dark" else "light"
        _cache = (now, mode)
        return mode


def wanted(choice: Any) -> str | None:
    """The mode ``choice`` asks for, or None for whichever Obsidian is showing."""
    if choice == "system":
        return system_mode()
    return choice if choice in ("light", "dark") else None


def snapshots(storage: Any, root: Path, choice: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The reading-view and file-explorer snapshots to wear for ``choice``, one mode for both."""
    mode = wanted(choice)
    markdown = storage.markdown_theme(root, mode) if mode else None
    if markdown is None:
        # Obsidian's own mode: asked for, or the only one measured so far.
        return storage.markdown_theme(root), storage.sidebar_theme(root)
    return markdown, storage.sidebar_theme(root, mode)


def available(storage: Any, root: Path) -> list[str]:
    """The modes the plugin has measured for this vault's reading view."""
    return [mode for mode in ("light", "dark") if storage.markdown_theme(root, mode) is not None]

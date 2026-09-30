"""Frozen application config + the folder-allowlist security check.

The allowlist is the single most important security boundary in this server:
it decides which directories spawned model-provider processes are allowed to read.
The check is deliberately done *after* ``Path.resolve()` (which follows symlinks)
and uses ``Path.is_relative_to`` rather than string prefixing — ``str.startswith``
would let ``~/Projects-evil`` slip past an allowed ``~/Projects`` root.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    default_folder: Path
    allowed_roots: tuple[Path, ...]
    model: str = "sonnet"
    host: str = "127.0.0.1"
    port: int = 8899
    allow_any: bool = False
    data_dir: Path | None = None
    # Adopt a vault and create the Artifacts folder on a new Mac (first_run.adopt_folders). Only the real entry
    # point turns it on, so a test's app never creates folders in the home directory.
    first_run: bool = False
    # Let mirror.toml start the phone mirror's publisher (mirror/service.py). Only the real entry point turns it on,
    # so no test's app ever reads the owner's mirror.toml, and without the file even that starts nothing.
    mirror: bool = False
    # Per-server random secret, baked into ask.js at serve time and required in
    # the /ask body. Defense-in-depth behind the Origin + Host checks.
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def resolve_allowed(self, folder: str | None) -> Path | None:
        """Resolve ``folder`` and return it only if it is an allowed directory.

        Returns the resolved ``Path`` on success, or ``None`` if the path does
        not resolve, is not a directory, or escapes every allowed root.
        """
        if not folder:
            return None
        try:
            resolved = Path(folder).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_dir():
            return None
        if self.allow_any:
            return resolved
        for root in self.allowed_roots:
            try:
                if resolved == root or resolved.is_relative_to(root):
                    return resolved
            except ValueError:
                continue
        return None

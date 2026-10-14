"""Open a dedicated Claude or Codex session in a terminal, scoped to a folder.

Write a seed prompt and a launch script, then open the selected subscription CLI
inside the context folder. Ghostty is preferred; iTerm and Terminal are fallbacks.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from .providers import find_claude, find_codex

_TERMINALS = [
    ("/Applications/Ghostty.app", "ghostty"),
    (str(Path.home() / "Applications/Ghostty.app"), "ghostty"),
    ("/Applications/iTerm.app", "open"),
    ("/System/Applications/Utilities/Terminal.app", "open"),
    ("/Applications/Utilities/Terminal.app", "open"),
]

def _find_terminal() -> tuple[str, str] | None:
    for path, kind in _TERMINALS:
        if Path(path).exists():
            return path, kind
    return None


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def open_in_provider(provider: str, folder: Path, prompt: str) -> None:
    """Launch an interactive subscription-backed provider session."""
    if provider == "codex":
        executable = find_codex()
        invocation = 'unset OPENAI_API_KEY CODEX_API_KEY\nexec {exe} -s read-only "$(cat {pf})"'
    else:
        executable = find_claude()
        invocation = (
            "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK "
            "CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY\n"
            'exec {exe} --strict-mcp-config "$(cat {pf})"'
        )
    if not executable:
        raise RuntimeError(f"The `{provider}` CLI was not found. Install it and sign in with your subscription.")
    term = _find_terminal()
    if not term:
        raise RuntimeError("No terminal found (Ghostty / iTerm / Terminal).")
    term_path, kind = term

    scratch = Path.home() / ".onyx" / "sessions" / uuid.uuid4().hex[:8]
    scratch.mkdir(parents=True, exist_ok=True)
    prompt_file = scratch / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    launch = scratch / "launch.command"
    launch.write_text(
        "#!/bin/zsh -l\nset -e\ncd {folder}\n{invocation}\n".format(
            folder=_shq(str(folder)),
            invocation=invocation.format(exe=_shq(executable), pf=_shq(str(prompt_file))),
        ),
        encoding="utf-8",
    )
    launch.chmod(0o755)

    router = Path.home() / ".local/bin/cx-open-in-herdr"
    if provider == "claude" and router.is_file():
        result = subprocess.run(
            [str(router), "--cwd", str(folder), "--launch", str(launch),
             "--tab", "Onyx · " + folder.name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not open Claude in Herdr.")
        return

    if kind == "ghostty":
        # `open -na` forces a NEW Ghostty instance (so our `-e` command is honored
        # even when Ghostty is already running). On macOS a fresh instance also
        # RESTORES the previous session's saved windows — so the user got the
        # restored window(s) *plus* our command window (the duplicate tab).
        # `--window-save-state=never` is a per-launch override that tells this
        # instance not to restore or save state, so exactly one tab opens. It does
        # not touch the user's config or their primary Ghostty instance.
        # See cxmail claude_handoff.rs / ghostty-org/ghostty#9612.
        cmd = [
            "/usr/bin/open", "-na", term_path, "--args",
            "--window-save-state=never",
            f"--working-directory={folder}", "-e", str(launch),
        ]
    else:  # iTerm / Terminal.app run an executable .command when opened
        cmd = ["/usr/bin/open", "-a", term_path, str(launch)]

    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def open_in_claude(folder: Path, prompt: str) -> None:
    """Backward-compatible Claude handoff."""
    open_in_provider("claude", folder, prompt)

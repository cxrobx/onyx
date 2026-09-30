"""CLI entrypoint: ``python -m onyx --folder ... --port 8899``."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

from .app import create_app
from .config import AppConfig


def _under(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:
        return False


def _watch_parent(parent_pid: int) -> None:
    """Exit if the native launcher disappears without terminating its child.

    A normal app quit still sends SIGTERM. This guard covers force-quit/crash so
    a stale server does not survive indefinitely and get mistaken for a fresh
    app instance on the next launch.
    """
    while True:
        time.sleep(2)
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            os._exit(0)
        except PermissionError:
            pass  # The process exists; we just cannot signal it.


def main() -> None:
    if sys.argv[1:2] == ["mirror"]:
        # `onyx mirror …` is its own command line (the phone mirror); the server's flags don't apply to it.
        from .mirror.cli import main as mirror_main

        raise SystemExit(mirror_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(
        prog="onyx",
        description="Local server for the highlight-to-ask reading companion widget.",
    )
    parser.add_argument(
        "--folder",
        default=str(Path.home() / "Projects"),
        help="Default context folder the selected provider may read. Default: ~/Projects",
    )
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host. Keep this loopback; anything else exposes the model runners.",
    )
    parser.add_argument("--model", default="sonnet", help="Model passed to `claude --model`.")
    parser.add_argument(
        "--allow-root",
        action="append",
        default=[],
        metavar="DIR",
        help="Extra root the widget may point at (repeatable). Default root is ~/Projects.",
    )
    parser.add_argument(
        "--allow-any",
        action="store_true",
        help="LOUD escape hatch: allow any readable directory. Disables the folder allowlist.",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override the application data directory (primarily for development/tests).",
    )
    args = parser.parse_args()

    if args.parent_pid and args.parent_pid > 1 and args.parent_pid != os.getpid():
        threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()

    default_folder = Path(args.folder).expanduser().resolve()

    roots: list[Path] = [(Path.home() / "Projects").resolve()]
    roots += [Path(r).expanduser().resolve() for r in args.allow_root]
    # Make sure the chosen default folder is itself reachable through the allowlist.
    if not any(_under(default_folder, r) for r in roots):
        roots.append(default_folder)
    roots = list(dict.fromkeys(roots))  # dedupe, preserve order

    config = AppConfig(
        default_folder=default_folder,
        allowed_roots=tuple(roots),
        model=args.model,
        host=args.host,
        port=args.port,
        allow_any=args.allow_any,
        data_dir=Path(args.data_dir).expanduser().resolve() if args.data_dir else None,
        first_run=True,
        mirror=True,
    )
    app = create_app(config)

    roots_label = "(any — --allow-any)" if args.allow_any else ", ".join(str(r) for r in roots)
    print(
        "\n  Onyx server\n"
        f"  Folder:  {default_folder}\n"
        f"  Roots:   {roots_label}\n"
        f"  Model:   {config.model}\n"
        f"  URL:     http://{args.host}:{args.port}\n"
        f'  Embed:   <script src="http://{args.host}:{args.port}/ask.js"></script>\n'
    )
    if args.host not in ("127.0.0.1", "localhost"):
        print(
            "  WARNING: binding to a non-loopback host exposes the read-only model\n"
            "           runners to your network. Only do this on a trusted network.\n"
        )

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

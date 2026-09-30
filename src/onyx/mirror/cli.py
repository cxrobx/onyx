"""``onyx mirror …``: set the phone mirror up, run it by hand, pair a phone, turn it off.

No subcommand prints a credential. ``pair`` shows the pairing string only as a QR code (in Preview, which
is closed and deleted again after two minutes) or hands it to the clipboard; the string itself is never
written to a terminal, because a terminal's scrollback is how a secret ends up somewhere it wasn't sent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as stdlib_secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import storage as app_storage
from . import config as mirror_config
from . import secrets as keychain
from .crypto import b64url_decode, b64url_encode, generate_master
from .errors import MirrorCryptoError, MirrorDependencyError, MirrorError, MirrorNotConfigured, describe_error
from .publish import clear_state, last_published, locked, publish
from .store import LocalDirStore, R2Store, _ACCOUNT_RE, _BUCKET_RE

PAIRING_PREFIX = "onyxmirror1:"
QR_LIFETIME_SECONDS = 120
_TOKEN_RE = re.compile(r"[\x21-\x7e]{16,}")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9]{8,128}")
_LOCAL_HOSTS = ("http://127.0.0.1", "http://localhost")


def _say(text: str = "") -> None:
    print(text)


def _fail(text: str) -> None:
    print(f"onyx mirror: {text}", file=sys.stderr)


def validate(account: str, value: str) -> str:
    """The value cleaned, or a ``MirrorNotConfigured`` that names the account and never the value."""
    value = value.strip()
    if account == keychain.WORKER_URL:
        value = value.rstrip("/")
        if not (value.startswith("https://") or value.startswith(_LOCAL_HOSTS)) or re.search(r"\s", value):
            raise MirrorNotConfigured("worker_url must be an https:// URL")
    elif account == keychain.MASTER_KEY:
        try:
            ok = len(b64url_decode(value)) == 32
        except MirrorCryptoError:
            ok = False
        if not ok:
            raise MirrorNotConfigured("master_key must be 32 random bytes, base64url")
    elif account == keychain.READ_TOKEN:
        if not _TOKEN_RE.fullmatch(value):
            raise MirrorNotConfigured("read_token must be at least 16 printable characters with no spaces")
    elif account == keychain.R2_ACCOUNT_ID:
        if not _ACCOUNT_RE.fullmatch(value):
            raise MirrorNotConfigured("r2_account_id does not look like a Cloudflare account id")
    elif account == keychain.R2_BUCKET:
        if not _BUCKET_RE.fullmatch(value):
            raise MirrorNotConfigured("r2_bucket does not look like a bucket name")
    elif account == keychain.R2_ACCESS_KEY_ID:
        if not _KEY_ID_RE.fullmatch(value):
            raise MirrorNotConfigured("r2_access_key_id does not look like an R2 access key id")
    elif account == keychain.R2_SECRET_ACCESS_KEY:
        if not _TOKEN_RE.fullmatch(value):
            raise MirrorNotConfigured("r2_secret_access_key must be at least 16 printable characters with no spaces")
    return value


def pairing_string(secrets: keychain.Secrets) -> str:
    """``onyxmirror1:`` + base64url of ``{"u": worker URL, "t": read token, "k": master key}``."""
    values = {a: secrets.get(a) for a in keychain.PAIR_ACCOUNTS}
    missing = [a for a, v in values.items() if not v]
    if missing:
        raise MirrorNotConfigured("missing in the Keychain: " + ", ".join(missing) + " (run `onyx mirror setup`)")
    body = {"u": values[keychain.WORKER_URL].rstrip("/"), "t": values[keychain.READ_TOKEN],
            "k": values[keychain.MASTER_KEY]}
    return PAIRING_PREFIX + b64url_encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))


def _open_storage(data_dir: Path | None):
    """The app's own storage. With no explicit directory this is ``Storage(None)``, not ``Storage(default_data_dir())``:
    only the former adopts a pre-rename database, and creating an empty one here first would strand it."""
    return app_storage.Storage(None if data_dir == app_storage.default_data_dir() else data_dir)


# -- subcommands -----------------------------------------------------------------------------------------

def cmd_status(args, secrets, data_dir) -> int:
    path = mirror_config.path_for(data_dir)
    config = mirror_config.load(data_dir)
    if config:
        _say(f"Phone mirror: on, every {config.interval_minutes} min")
        _say("  include: " + ", ".join(config.include))
        if config.exclude:
            _say("  exclude: " + ", ".join(config.exclude))
    elif path.exists():
        _say("Phone mirror: off (mirror.toml needs `enabled = true` and a non-empty include list)")
    else:
        _say("Phone mirror: not set up (no mirror.toml)")
    last = last_published(data_dir)
    if last:
        _say(f"Last publish: {last['at']}, {last['pages']} pages and {last['assets']} assets "
             f"({last['uploaded']} uploaded, {last['deleted']} deleted, {last['bytes']} bytes)")
    else:
        _say("Last publish: none on record")
    _say("Keychain (service onyx-mirror):")
    for account in keychain.ACCOUNTS:
        _say(f"  {account}: {'set' if secrets.has(account) else 'missing'}")
    return 0


def _prompt_values(secrets, skip: set[str]) -> dict[str, str]:
    values = {}
    for account in keychain.ACCOUNTS:
        if account in skip:
            continue
        hint = " (set; Enter keeps it)" if secrets.has(account) else " (Enter skips)"
        answer = getpass.getpass(f"{account}{hint}: ").strip()
        if answer:
            values[account] = answer
    return values


def cmd_setup(args, secrets, data_dir) -> int:
    values: dict[str, str] = {}
    generated: list[str] = []
    if args.generate:
        for account, make in ((keychain.MASTER_KEY, generate_master),
                              (keychain.READ_TOKEN, lambda: stdlib_secrets.token_urlsafe(32))):
            if not secrets.has(account):
                values[account] = make()
                generated.append(account)
    if args.from_stdin:
        try:
            given = json.loads(sys.stdin.read())
        except ValueError:
            _fail("stdin is not JSON")
            return 2
        if not isinstance(given, dict) or not all(isinstance(v, str) for v in given.values()):
            _fail("stdin must be one JSON object of account name to string value")
            return 2
        if any(k not in keychain.ACCOUNTS for k in given):
            _fail("stdin names an account this doesn't know; the accounts are " + ", ".join(keychain.ACCOUNTS))
            return 2
        values.update(given)
    else:
        values.update(_prompt_values(secrets, skip=set(generated)))
    cleaned = {account: validate(account, value) for account, value in values.items()}
    for account, value in cleaned.items():
        replaced_key = account == keychain.MASTER_KEY and account not in generated and secrets.has(account)
        secrets.set(account, value)
        _say(f"{'Generated' if account in generated else 'Saved'} {account}.")
        if replaced_key:
            _say("  The master key changed: paired phones can't read the old copy, so pair them again. "
                 "The next publish re-uploads everything and deletes what the old key made.")
    if mirror_config.write_template(data_dir):
        _say(f"Wrote {mirror_config.path_for(data_dir)} (off). Set include, then enabled = true, to turn the mirror on.")
    missing = [a for a in keychain.ACCOUNTS if not secrets.has(a)]
    if missing:
        _say("Still missing: " + ", ".join(missing))
    return 0


def _r2(secrets) -> R2Store:
    return R2Store.from_secrets(secrets)


def cmd_publish(args, secrets, data_dir) -> int:
    config = mirror_config.load(data_dir)
    if config is None:
        _fail("the mirror is off. mirror.toml must say `enabled = true` and list what to publish in `include`.")
        return 2
    from . import service

    store = LocalDirStore(Path(args.to)) if args.to else _r2(secrets)
    db = _open_storage(data_dir)
    try:
        roots = service.roots_from_settings(db.settings())
        report = publish(config=config, secrets=secrets, store=store, roots=roots,
                         markdown_css=service.markdown_css_for(db, roots), data_dir=db.data_dir,
                         look=service.look_for(db, roots),
                         library=service.library_for(db, config, service.markdown_css_for(db, roots)))
    finally:
        db.close()
    _say(f"Published {report.pages} pages and {report.assets} assets: {report.uploaded} uploaded "
         f"({report.bytes} bytes), {report.unchanged} unchanged, {report.deleted} deleted.")
    if args.to:
        _say("That was a dry run into a local folder; nothing was sent over the network.")
    return 0


def _cleanup_later(folder: Path) -> None:
    # Detached into its own session so it outlives this command, which is gone long before the QR is.
    subprocess.Popen(["/bin/sh", "-c", 'sleep "$1"; rm -rf -- "$2"', "sh", str(QR_LIFETIME_SECONDS), str(folder)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _qr(text: str):
    try:
        import segno
    except ImportError as exc:
        raise MirrorDependencyError("The phone mirror needs `pip install onyx[mirror]`.") from exc
    return segno.make(text, error="m")


def cmd_pair(args, secrets, data_dir) -> int:
    text = pairing_string(secrets)
    if args.copy:
        try:
            subprocess.run(["/usr/bin/pbcopy"], input=text, text=True, check=True)
        except (subprocess.CalledProcessError, OSError):
            _fail("couldn't reach the clipboard")
            return 1
        _say("Copied.")
    elif args.terminal:
        _qr(text).terminal(compact=True)
    else:
        code = _qr(text)
        folder = Path(tempfile.mkdtemp(prefix="onyx-pair-"))  # mkdtemp makes it 0700
        try:
            image = folder / "pairing.png"
            code.save(str(image), kind="png", scale=8, border=4)
            opened = subprocess.run(["/usr/bin/open", str(image)])
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        if opened.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            _fail("couldn't open the QR code")
            return 1
        _cleanup_later(folder)
        _say("QR opened in Preview; it's deleted in 2 minutes.")
    return 0


def cmd_stop(args, secrets, data_dir) -> int:
    if mirror_config.disable(data_dir):
        _say("Publishing is off (enabled = false in mirror.toml). A running Onyx stops at its next cycle.")
    else:
        _say("Nothing to stop: there is no mirror.toml.")
    return 0


def cmd_wipe(args, secrets, data_dir) -> int:
    store = _r2(secrets)
    if not args.yes:
        if input("This deletes every object in the mirror bucket. Type wipe to confirm: ").strip() != "wipe":
            _say("Not confirmed; nothing was deleted.")
            return 1
    with locked(data_dir):
        ids = store.list_ids()
        for id_ in ids:
            store.delete(id_)
        clear_state(data_dir, store.destination)
    _say(f"Deleted {len(ids)} objects and cleared the local record.")
    if mirror_config.load(data_dir):
        _say("Publishing is still on, so the next publish uploads again; run `onyx mirror stop` to end that.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onyx mirror", description="The read-only phone mirror.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Is it on, when did it last publish, which Keychain accounts are set (names only).")

    setup = sub.add_parser("setup", help="Put the credentials in the Keychain and write a disabled mirror.toml.")
    setup.add_argument("--from-stdin", action="store_true",
                       help='Read one JSON object of account name to value from stdin instead of prompting.')
    setup.add_argument("--generate", action="store_true",
                       help="Create master_key and read_token if they are not set. Their values are never printed.")

    pub = sub.add_parser("publish", help="Publish now. Needs mirror.toml to say enabled = true.")
    pub.add_argument("--to", metavar="DIR", help="Write the sealed objects into DIR instead of the bucket (a dry run).")

    pair = sub.add_parser("pair", help="Show the QR code that pairs a phone.")
    how = pair.add_mutually_exclusive_group()
    how.add_argument("--copy", action="store_true",
                     help="Copy the pairing string to the clipboard instead. Clipboard managers and Universal "
                          "Clipboard may keep or sync it.")
    how.add_argument("--terminal", action="store_true",
                     help="Draw the QR code in this terminal. Only for your own terminal: anything that reads "
                          "its output (a log, an agent) then holds the pairing secret.")

    sub.add_parser("stop", help="Turn publishing off (enabled = false).")

    wipe = sub.add_parser("wipe", help="Delete every object in the bucket.")
    wipe.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    return parser


COMMANDS = {"status": cmd_status, "setup": cmd_setup, "publish": cmd_publish, "pair": cmd_pair,
            "stop": cmd_stop, "wipe": cmd_wipe}


def main(argv: list[str] | None = None, *, secrets: keychain.Secrets | None = None,
         data_dir: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    secrets = secrets if secrets is not None else keychain.KeychainSecrets()
    data_dir = data_dir or app_storage.default_data_dir()
    try:
        return COMMANDS[args.command](args, secrets, data_dir)
    except MirrorNotConfigured as exc:
        _fail(str(exc))
        return 2
    except MirrorError as exc:
        _fail(describe_error(exc))
        return 1
    except Exception as exc:
        # A library's message can quote an endpoint or a path; only its type is shown unless asked.
        if os.environ.get("ONYX_MIRROR_DEBUG"):
            raise
        _fail(f"unexpected {type(exc).__name__} (set ONYX_MIRROR_DEBUG=1 for the traceback)")
        return 1

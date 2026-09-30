"""Where the mirror's credentials live: the login Keychain, service ``onyx-mirror``, one item per account.

A value is never on a command line. Writes go through ``security -i``, which reads its commands from
stdin, so the value shows up in neither ``ps`` nor the process list (an argv is readable by every other
process of the same user, and ``security add-generic-password -w <value>`` puts it there). Reads put
only service and account *names* on argv.

Nothing here logs, prints or reprs a value; the classes' reprs name accounts and nothing else.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from .errors import SecretsError

SERVICE = "onyx-mirror"
SECURITY = "/usr/bin/security"

MASTER_KEY = "master_key"
READ_TOKEN = "read_token"
WORKER_URL = "worker_url"
R2_ACCOUNT_ID = "r2_account_id"
R2_BUCKET = "r2_bucket"
R2_ACCESS_KEY_ID = "r2_access_key_id"
R2_SECRET_ACCESS_KEY = "r2_secret_access_key"

ACCOUNTS = (
    MASTER_KEY,
    READ_TOKEN,
    WORKER_URL,
    R2_ACCOUNT_ID,
    R2_BUCKET,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
)
# What publishing needs. The read token and Worker URL are the phone's (pairing), not the Mac's.
PUBLISH_ACCOUNTS = (MASTER_KEY, R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)
PAIR_ACCOUNTS = (MASTER_KEY, READ_TOKEN, WORKER_URL)

_NOT_FOUND = 44  # `security`'s exit status for "the specified item could not be found"


class Secrets(Protocol):
    def get(self, account: str) -> str | None: ...
    def set(self, account: str, value: str) -> None: ...
    def has(self, account: str) -> bool: ...
    def delete(self, account: str) -> None: ...


def _check_account(account: str) -> None:
    if account not in ACCOUNTS:
        raise SecretsError("unknown mirror account")


def _check_value(value: str) -> None:
    # Printable ASCII only. A newline would end the `security -i` command early and start another one;
    # `security -w` prints a non-ASCII value back as hex; an empty -w makes `security` prompt.
    if not value or not all(" " <= ch <= "~" for ch in value):
        raise SecretsError("a credential must be non-empty printable ASCII")


def _quote(value: str) -> str:
    """One word for `security -i`'s command parser: double-quoted, with backslash and quote escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class KeychainSecrets:
    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def __repr__(self) -> str:
        return f"KeychainSecrets(service={self.service!r})"

    def get(self, account: str) -> str | None:
        _check_account(account)
        done = subprocess.run(
            [SECURITY, "find-generic-password", "-s", self.service, "-a", account, "-w"],
            capture_output=True, text=True,
        )
        if done.returncode == _NOT_FOUND:
            return None
        if done.returncode != 0:
            raise SecretsError(f"the Keychain read of {account} failed (exit {done.returncode})")
        return done.stdout[:-1] if done.stdout.endswith("\n") else done.stdout

    def has(self, account: str) -> bool:
        """Whether the item exists, asked without `-w`, so the value is never read at all."""
        _check_account(account)
        done = subprocess.run(
            [SECURITY, "find-generic-password", "-s", self.service, "-a", account],
            capture_output=True, text=True,
        )
        return done.returncode == 0

    def set(self, account: str, value: str) -> None:
        _check_account(account)
        _check_value(value)
        command = f"add-generic-password -U -s {self.service} -a {account} -w {_quote(value)}\n"
        done = subprocess.run([SECURITY, "-i"], input=command, capture_output=True, text=True)
        if done.returncode != 0:
            # Not done.stderr: a tool's usage text is not something to echo next to a credential.
            raise SecretsError(f"the Keychain write of {account} failed (exit {done.returncode})")

    def delete(self, account: str) -> None:
        _check_account(account)
        subprocess.run(
            [SECURITY, "delete-generic-password", "-s", self.service, "-a", account],
            capture_output=True, text=True,
        )


class MemorySecrets:
    """The Keychain's stand-in for tests."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def __repr__(self) -> str:
        return f"MemorySecrets(accounts={sorted(self._values)})"

    def get(self, account: str) -> str | None:
        _check_account(account)
        return self._values.get(account)

    def has(self, account: str) -> bool:
        _check_account(account)
        return account in self._values

    def set(self, account: str, value: str) -> None:
        _check_account(account)
        _check_value(value)
        self._values[account] = value

    def delete(self, account: str) -> None:
        _check_account(account)
        self._values.pop(account, None)

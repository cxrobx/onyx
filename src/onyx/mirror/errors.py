"""Errors whose messages are safe to print or log.

A ``MirrorError`` message is written by this package and never carries a credential, a vault path or
an endpoint, so ``describe_error`` can show it as it is. Any other exception might (a library's
message often quotes the URL or the argument it choked on), so only its type is shown.
"""

from __future__ import annotations


class MirrorError(Exception):
    """Base for errors whose text is safe to show."""


class MirrorDependencyError(MirrorError):
    """The optional ``mirror`` extra is not installed."""


class MirrorNotConfigured(MirrorError):
    """Something the mirror needs (a Keychain account, an include list) has not been set up."""


class MirrorCryptoError(MirrorError):
    """A blob did not open, or a key was the wrong shape."""


class SecretsError(MirrorError):
    """The Keychain refused a read or a write."""


class StoreError(MirrorError):
    """The object store refused a request."""


def describe_error(exc: BaseException) -> str:
    """What may be logged or printed about ``exc``: its message if this package wrote it, else its type."""
    if isinstance(exc, MirrorError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__

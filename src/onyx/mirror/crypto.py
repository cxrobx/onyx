"""Wire format v1's cryptography (docs/plans/phone-mirror.md, "Wire format v1").

One random master key derives two: ``K_enc`` seals every blob and ``K_id`` names them, so Cloudflare sees
neither contents nor paths. The phone implements the same thing with CryptoKit; ``tests/fixtures/
mirror_vectors.json`` is what both sides must reproduce.

``cryptography`` is an opt-in extra (``pip install onyx[mirror]``), so it is imported inside the
functions that need it and nothing here touches it at import time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import zlib
from dataclasses import dataclass

from .errors import MirrorCryptoError, MirrorDependencyError

SALT = b"onyx-mirror/v1"
NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise MirrorCryptoError("not base64url") from exc


def generate_master() -> str:
    """A fresh master key, base64url with no padding."""
    return b64url_encode(os.urandom(KEY_SIZE))


def deflate_raw(data: bytes) -> bytes:
    """RFC 1951 with no zlib header, which is what Apple's ``NSData.decompressed(using: .zlib)`` reads."""
    packer = zlib.compressobj(wbits=-15)
    return packer.compress(data) + packer.flush()


def inflate_raw(data: bytes) -> bytes:
    return zlib.decompress(data, -15)


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise MirrorDependencyError("The phone mirror needs `pip install onyx[mirror]`.") from exc
    return AESGCM(key)


@dataclass(frozen=True)
class Keys:
    enc: bytes
    id: bytes

    def __repr__(self) -> str:
        return "Keys(<hidden>)"

    @classmethod
    def derive(cls, master: bytes) -> "Keys":
        if len(master) != KEY_SIZE:
            raise MirrorCryptoError("the master key must be 32 bytes")
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        except ImportError as exc:
            raise MirrorDependencyError("The phone mirror needs `pip install onyx[mirror]`.") from exc

        def derive(info: bytes) -> bytes:
            return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=SALT, info=info).derive(master)

        return cls(enc=derive(b"enc"), id=derive(b"id"))

    @classmethod
    def from_b64url(cls, master: str) -> "Keys":
        return cls.derive(b64url_decode(master))

    def ident(self, name: str) -> str:
        """The object id for ``name``: 64 lowercase hex characters that reveal nothing about it."""
        return hmac.new(self.id, name.encode("utf-8"), hashlib.sha256).hexdigest()

    def seal(self, id: str, plaintext: bytes, nonce: bytes | None = None) -> bytes:
        """``nonce ‖ ciphertext ‖ tag``. The id is the associated data, so a store that swaps two objects
        makes both fail to open. ``nonce`` is fixed only by tests that reproduce the shared vectors."""
        nonce = os.urandom(NONCE_SIZE) if nonce is None else nonce
        if len(nonce) != NONCE_SIZE:
            raise MirrorCryptoError("a nonce is 12 bytes")
        return nonce + _aesgcm(self.enc).encrypt(nonce, plaintext, id.encode("ascii"))

    def open_blob(self, id: str, blob: bytes) -> bytes:
        if len(blob) < NONCE_SIZE + TAG_SIZE:
            raise MirrorCryptoError("a blob is shorter than its nonce and tag")
        try:
            return _aesgcm(self.enc).decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], id.encode("ascii"))
        except MirrorDependencyError:
            raise
        except Exception as exc:  # cryptography's InvalidTag carries no message worth keeping
            raise MirrorCryptoError("the blob does not open under this key and id") from exc
